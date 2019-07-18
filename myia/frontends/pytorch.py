"""PyTorch Frontend."""

import torch
import torch.utils.dlpack

import copy

from .. import composite as C
from ..composite import core
from ..prim import ops as P
from ..abstract.infer import to_abstract, ArrayWrapper
from ..abstract.data import AbstractClassBase, AbstractArray, AbstractScalar, \
    ANYTHING, VALUE, TYPE, SHAPE
from ..dtype import Int, UInt, Float, Bool, Number
from ..pipeline.resources import standard_object_map, standard_method_map
from ..hypermap import hyper_map
from ..api import _convert_arg_init
from ..pipeline.steps import convert_arg, convert_result_array

from .pytorch_functions import linear, tensor_dim, t


_type_map = {
    torch.int8: Int[8],
    torch.int16: Int[16],
    torch.int32: Int[32],
    torch.int64: Int[64],
    torch.uint8: UInt[8],
    torch.float16: Float[16],
    torch.float32: Float[32],
    torch.float64: Float[64],
    torch.uint8: Bool,
}


def pytorch_dtype_to_type(dtype):
    """Map a pytorch dtype to a myia type."""
    if dtype not in _type_map:
        raise TypeError(f"Unsupported dtype {dtype}")
    return _type_map[dtype]


standard_object_map.update({
    torch.nn.functional.linear: linear,
    torch.tanh: C.tanh,
})


class AbstractModule(AbstractClassBase):
    """Represents a PyTorch Module."""

    def user_defined_version(self):
        """Return the user-defined version of this type.

        This uses the attribute types as defined by the user, rather than what
        is generated by the inferrer or other methods.

        Current default is to return self in order to make it easier for Myia
        hypermap mapping function to return a different type from its input
        (especially for pytorch modules and their contents).
        """
        return self


class AbstractPyTorchTensor(AbstractArray):
    """Represents a PyTorch Tensor."""

    def __init__(self, element, values, requires_grad=None, retain_grad=None):
        """Initialize an AbstractPyTorchTensor."""
        super().__init__(element, values)
        self.requires_grad = requires_grad

        # TODO: IS RETAIN_GRAD EVEN NEEDED? IDK YET.
        self.retain_grad = retain_grad


# class AbstractPyTorchParameter(AbstractPyTorchTensor):
#     """Represents a PyTorch Parameter."""

#     # This might become an alternative way to log that this is a Parameter.


class PyTorchTensorWrapper(ArrayWrapper):
    """Represents a PyTorch Tensor wrapped by to_device."""

    def __init__(self, array, dtype, shape, backend,
                 requires_grad=None, retain_grad=None):
        """Initialize the PyTorchTensorWrapper."""
        super().__init__(array, dtype, shape, backend)
        self.requires_grad = requires_grad

        # TODO: IS RETAIN_GRAD EVEN NEEDED? IDK YET.
        self.retain_grad = retain_grad


AT = AbstractPyTorchTensor(ANYTHING, {SHAPE: ANYTHING})


C.tanh.register(AT)(C.array_tanh)


@C._leaf_zeros_like.register(AT)
@core
def _array_zero(xs):
    scalar_zero = P.scalar_cast(0, C.typeof(xs).element)
    return P.distribute(C.to_array(scalar_zero, C.typeof(xs)),
                        P.shape(xs))


standard_method_map[AbstractPyTorchTensor] = \
    standard_method_map[AbstractArray].copy()
standard_method_map[AbstractPyTorchTensor].update({
    'dim': tensor_dim,
    't': t,
    'tanh': C.tanh,
    'zeros_like': C.zeros_like,
})


@core
def mod_sub(self, x):
    """Hypermap subtraction."""
    return hyper_map(C.sub, self, x)

##############################################################################


# # This might end up as an alternative to blacklist of Module constructors.
# # I.e. get the list of constructors from a dummy pytorch module.
# class DummyModule(nn.Module):
#     def __init__(self):
#         super(Model, self).__init__()

#     def forward(self, x):
#        return 9

# dummy_module =  DummyModule()


# TODO: should all of these actually be blacklisted (not used).
# Curently blacklists all constructors except '_parameters' and '_modules'.
# 'training' should probably be removed from blacklist in next PR.
blacklist = ('_backend', '_buffers', '_backward_hooks', '_forward_hooks',
             '_forward_pre_hooks', '_state_dict_hooks',
             '_load_state_dict_pre_hooks',

             'training'
             )


@to_abstract.register
def _to_abstract(self, v: torch.nn.Module, context, ref, loop):
    fwd_fn = getattr(type(v), 'forward')
    attrs = {}
    for var_k, var_v in vars(v).items():
        if var_k not in blacklist:

            # TODO: Remove this if statement line once Dict PR is ready
            if var_k not in ('_parameters', '_modules'):

                attrs[var_k] = self(var_v)
        else:
            pass
            # TODO: maybe make a warning for if user happened
            #       to name attribute something in blacklist

    # """ TODO: remove these 2 loops (module and params) once Dict PR is ready
    for mod_k, mod_v in v._modules.items():
        attrs[mod_k] = self(mod_v)

    for par_k, par_v in v._parameters.items():
        attrs[par_k] = self(par_v)
        # """

    # TODO: figure out how to delattr so that memory doesn't double
    # for k in attrs:
    #     delattr(v, k)

    names = list(attrs.keys())

    def new_module(*args):
        nonlocal v
        # TODO: Figure out something more memory efficient than deepcopy.
        #       P.S. We tried copy.copy(v) and it is not sufficiently deep.
        v = copy.deepcopy(v)
        for k, a in zip(names, args):
            if isinstance(a, ArrayWrapper):
                # Pre_conversion from backend to PyTorch seems to only be
                # necessary when _convert_arg_init of to_device builds
                # via constructors with args that are ArrayWrappers
                # (and the backend used is not PyTorch).
                # (backend is checked via whether a.array is a torch.Tensor)
                if not isinstance(a.array, torch.Tensor):
                    a = torch.utils.dlpack.from_dlpack(
                        a.backend.to_dlpack(a.array))
                else:
                    a = a.array

            if isinstance(getattr(v, k), torch.nn.Parameter):
                setattr(v, k, torch.nn.Parameter(a))
            else:
                setattr(v, k, a)
        return v

    return AbstractModule(v.__class__, attrs, {'__call__': fwd_fn,
                          '__sub__': mod_sub}, constructor=new_module)


@to_abstract.register  # noqa: F811
def _to_abstract(self, v: torch.Tensor, context, ref, loop):
    return AbstractPyTorchTensor(
        AbstractScalar({
            VALUE: ANYTHING,
            TYPE: pytorch_dtype_to_type(v.dtype),
        }),
        {SHAPE: tuple(v.shape)},
        v.requires_grad,
        v.retain_grad
    )


@to_abstract.register  # noqa: F811
def _to_abstract(self, v: torch.nn.Parameter, context, ref, loop):
    # return AbstractPyTorchParameter(
    return AbstractPyTorchTensor(
        AbstractScalar({
            VALUE: ANYTHING,
            TYPE: pytorch_dtype_to_type(v.dtype),
        }),
        {SHAPE: tuple(v.shape)},
        v.requires_grad,
        v.retain_grad
    )


@to_abstract.register  # noqa: F811
def _to_abstract(self, v: PyTorchTensorWrapper, context, ref, loop):
    return AbstractPyTorchTensor(
        AbstractScalar({
            VALUE: ANYTHING,
            TYPE: pytorch_dtype_to_type(v.dtype),
        }),
        {SHAPE: tuple(v.shape)},
        v.requires_grad,
        v.retain_grad
    )

##############################################################################


@_convert_arg_init.register
def _pt__convert_arg_init(self, arg, orig_t: AbstractPyTorchTensor, backend):
    et = orig_t.element
    assert isinstance(et, AbstractScalar)
    et = et.values[TYPE]
    assert issubclass(et, Number)
    if isinstance(arg, torch.Tensor):
        arg = PyTorchTensorWrapper(
            backend.from_dlpack(torch.utils.dlpack.to_dlpack(arg)),
            arg.dtype, arg.shape, backend, arg.requires_grad, arg.retain_grad)
    return arg

##############################################################################


@convert_arg.register
def _convert_arg(self, arg, orig_t: AbstractPyTorchTensor, backend):
    et = orig_t.element
    assert isinstance(et, AbstractScalar)
    et = et.values[TYPE]
    assert issubclass(et, Number)
    if isinstance(arg, ArrayWrapper):
        arg = arg.array
    if isinstance(arg, torch.Tensor):
        arg = backend.from_dlpack(torch.utils.dlpack.to_dlpack(arg))
    backend.check_array(arg, et)
    return arg

##############################################################################


@convert_result_array.register
def _convert_result_array(arg, orig_t: AbstractPyTorchTensor, backend):
    if not isinstance(arg, torch.Tensor):
        arg = torch.utils.dlpack.from_dlpack(backend.to_dlpack(arg))
    return arg

##############################################################################
