"""Abstract Types for PyTorch Frontend."""

from ..abstract.infer import ArrayWrapper
from ..abstract.data import AbstractClassBase, AbstractArray, \
    ANYTHING, SHAPE


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


APT = AbstractPyTorchTensor(ANYTHING, {SHAPE: ANYTHING})