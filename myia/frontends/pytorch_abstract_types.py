"""Abstract Types for PyTorch Frontend."""

from ..abstract.data import (
    ANYTHING,
    SHAPE,
    TYPE,
    VALUE,
    AbstractArray,
    AbstractClassBase,
    AbstractScalar,
)
from ..utils import MyiaInputTypeError
from ..xtype import Bool, Float, Int, Object, UInt


class PyTorchTensor(Object):
    """Type of an AbstractArray that behaves like a PyTorch Tensor."""

    @classmethod
    def to_numpy(self, x):
        """Convert torch Tensor x to numpy."""
        import torch

        if not isinstance(x, torch.Tensor):
            raise MyiaInputTypeError(f"Expected torch.Tensor but got {x}.")
        return x.detach().cpu().numpy()

    @classmethod
    def from_numpy(self, x):
        """Convert numpy array x to a torch Tensor."""
        import torch

        return torch.from_numpy(x)


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
        return AbstractModule(
            self.tag,
            {attr: ANYTHING for attr in self.attributes},
            constructor=self.constructor,
        )


def pytorch_dtype_to_type(dtype):
    """Map a pytorch dtype to a myia type."""
    import torch

    _type_map = {
        torch.int8: Int[8],
        torch.int16: Int[16],
        torch.int32: Int[32],
        torch.int64: Int[64],
        torch.uint8: UInt[8],
        torch.float16: Float[16],
        torch.float32: Float[32],
        torch.float64: Float[64],
        torch.bool: Bool,
    }
    if dtype not in _type_map:
        raise TypeError(f"Unsupported dtype {dtype}")
    return _type_map[dtype]


APT = AbstractArray(
    AbstractScalar({TYPE: ANYTHING, VALUE: ANYTHING}),
    {SHAPE: ANYTHING, TYPE: PyTorchTensor},
)


APT_bool = AbstractArray(
    AbstractScalar({TYPE: Bool, VALUE: ANYTHING}),
    {SHAPE: ANYTHING, TYPE: PyTorchTensor},
)


AS = AbstractScalar({TYPE: ANYTHING, VALUE: ANYTHING})


__all__ = ["AbstractModule", "PyTorchTensor", "pytorch_dtype_to_type"]
