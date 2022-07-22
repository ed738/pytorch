from torchgen.model import (
    FunctionSchema,
    BaseTy,
    BaseType,
    NativeFunctionsViewGroup,
    NativeFunction,
    Argument,
)
from torchgen.api.types import (
    Binding,
    NamedCType,
    ConstRefCType,
    BaseCType,
    CType,
    tensorT,
    longT,
    boolT,
)
from torchgen.api import dispatcher
from typing import List, Optional


# This file describes the translation of JIT schema to API's used
# when creating view lambdas that are used by the functionalization pass.
# There are two types of lambdas: forward lambdas and reverse lambdas.
# These API's mostly follow the dispatcher API, with a few quirks:
# - The lambda capture has to convert reference types to value types
# - While the forward lambda just directly calls into the at::_ops API
#   (following the dispatcher convention), the logic here for the reverse lambda
#   is responsible for generating both the call-site, and the declarations
#   (which are implemented manually in the at::functionalization::impl namespace).

# The lambdas generated for each view op in the functionalization pass are of the form
# [capture_arguments](outer_arguments) -> returns_type {
#     return name(inner_arguments);
# }

# Define some specific lambda input arguments.
base_binding = Binding(
    name="base",
    nctype=NamedCType(name="base", type=ConstRefCType(BaseCType(tensorT))),
    argument=Argument(
        name="base", type=BaseType(BaseTy.Tensor), default=None, annotation=None
    ),
    default=None,
)
mutated_view_binding = Binding(
    name="mutated_view",
    nctype=NamedCType(name="mutated_view", type=ConstRefCType(BaseCType(tensorT))),
    argument=Argument(
        name="base", type=BaseType(BaseTy.Tensor), default=None, annotation=None
    ),
    default=None,
)
mutated_view_idx_binding = Binding(
    name="mutated_view_idx",
    nctype=NamedCType(name="mutated_view_idx", type=BaseCType(longT)),
    argument=Argument(
        name="base", type=BaseType(BaseTy.Tensor), default=None, annotation=None
    ),
    default=None,
)
reapply_views_binding = Binding(
    name="reapply_views",
    nctype=NamedCType(name="reapply_views", type=BaseCType(boolT)),
    argument=Argument(
        name="reapply_views", type=BaseType(BaseTy.bool), default=None, annotation=None
    ),
    default=None,
)

# The lambda capture itself doesn't have a name.
# The name returned here corresponds to the name of the inner function called by the lambda.
def name(
    g: NativeFunctionsViewGroup,
    *,
    is_reverse: bool,
    include_namespace: bool,
    reapply_views: Optional[bool] = None,
) -> str:
    if reapply_views is None:
        # reapply_views is only important for the fwd lambda,
        # since we always plumb the runtime "reapply_views" argument into the reverse function.
        assert is_reverse
    if is_reverse:
        # for the reverse: the name of the inverse function always involves "view_copy",
        # and we plumb the "reapply_views" flag into that function.
        # (We could avoid doing that, but that would require writing out twice as many view inverse functions).
        assert g.view_copy is not None
        api_name = g.view_copy.func.name.unambiguous_name()
        # in the reverse case, we codegen both the call-sites (which need the full namespace) and the declarations (which don't)
        if include_namespace:
            return f"at::functionalization::FunctionalInverses::{api_name}_inverse"
        else:
            return f"{api_name}_inverse"
    # in the forward case, we just directly call into the at::_ops API (so we always need the namespace)
    assert include_namespace
    assert g.view_copy is not None
    api_name = (
        g.view.func.name.unambiguous_name()
        if reapply_views
        else g.view_copy.func.name.unambiguous_name()
    )
    return f"at::_ops::{api_name}::call"


def capture_arguments(f: NativeFunction, *, is_reverse: bool) -> List[Binding]:
    # capture arguments include all arguments except `self`.
    # Importantly, they don't include any C++ reference types (or else we'll get a dangling reference in the capture),
    # So any reference types (IntArrayRef) need to be converted to value types (vector<int64_t>)
    args = f.func.arguments.flat_all
    assert args[0].type == BaseType(BaseTy.Tensor)
    non_self_args = args[1:]
    non_self_value_bindings = [
        dispatcher.argument(
            a,
            remove_non_owning_ref_types=True,
            structured_type_override=f.part_of_structured_group,
        )
        for a in non_self_args
    ]
    all_bindings = [reapply_views_binding] + non_self_value_bindings
    return all_bindings


def returns_type(func: FunctionSchema) -> CType:
    # Assertion: all view ops return tensor-like outputs
    assert len(func.returns) >= 1
    for ret in func.returns:
        assert ret.type.is_tensor_like()
    # However, the return type of the lambda is always an individual tensor.
    # For multi-tensor outputs, each tensor needs to be tracked individually.
    return BaseCType(tensorT)


def outer_arguments(*, is_reverse: bool) -> List[Binding]:
    if is_reverse:
        return [base_binding, mutated_view_binding, mutated_view_idx_binding]
    else:
        return [base_binding, mutated_view_idx_binding]


def inner_call_index(func: FunctionSchema) -> Optional[Binding]:
    # For view ops that return multiple tensors (like `split`), we generate a separate lambda for each output.
    # When we replay a view op that returns multiple tensors, we need to index into the output appropriately
    if len(func.returns) > 1 or (
        len(func.returns) == 1 and func.returns[0].type.is_list_like()
    ):
        return mutated_view_idx_binding
    return None


def inner_arguments(f: NativeFunction, is_reverse: bool) -> List[Binding]:
    args = f.func.arguments.flat_all
    assert args[0].type == BaseType(BaseTy.Tensor)
    non_self_args = args[1:]
    # The forward lambda calls the at::_ops API, while the reverse lambda calls the view inverse API.
    # Both of these follow the dispatcher API.
    non_self_bindings = [
        dispatcher.argument(a, structured_type_override=f.part_of_structured_group)
        for a in non_self_args
    ]
    if not is_reverse:
        # the forward lambda swaps out the original tensor argument with the lambd arg "base"
        return [base_binding] + non_self_bindings
    else:
        # the reverse lambda does the same, but with an additional "mutated_view" arg
        # additionally, we have a calling convention: for view ops that return multiple tensor outputs
        # their corresponding view_inverse function takes in an additional index argument.
        index_binding = inner_call_index(f.func)
        if index_binding is not None:
            return [
                base_binding,
                mutated_view_binding,
                reapply_views_binding,
                index_binding,
            ] + non_self_bindings
        else:
            return [
                base_binding,
                mutated_view_binding,
                reapply_views_binding,
            ] + non_self_bindings
