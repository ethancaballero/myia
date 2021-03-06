"""Abstract Types for PyTorch Frontend."""

from ..abstract.data import ANYTHING, SHAPE, AbstractArray, AbstractClassBase
from ..abstract.infer import ArrayWrapper


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

    def __init__(self, element, values):
        """Initialize an AbstractPyTorchTensor."""
        super().__init__(element, values)

        """
        self.requires_grad = requires_grad

        # TODO: IS RETAIN_GRAD EVEN NEEDED? IDK YET.
        self.retain_grad = retain_grad

        If they are used at all, requires_grad and retain_grad should
        probably be tracks in self.values, e.g. self.values[REQUIRES_GRAD],
        where REQUIRES_GRAD is a Track object like VALUE or TYPE. That's
        because the current machinery to handle AbstractArray can already
        handle everything in values, whereas these extra fields will be
        ignored entirely, so they won't matter in equality comparisons,
        they will disappear whenever we try to copy an AbstractPyTorchTensor,
        etc.

        For the time being, I think I'd just axe them, until we have to
        actually do something with the information.
        """


# class AbstractPyTorchParameter(AbstractPyTorchTensor):
#     """Represents a PyTorch Parameter."""

#     # This might become an alternative way to log that this is a Parameter.


class PyTorchTensorWrapper(ArrayWrapper):
    """Represents a PyTorch Tensor wrapped by to_device."""

    def __init__(self, array, dtype, shape, backend):
        """Initialize the PyTorchTensorWrapper."""
        super().__init__(array, dtype, shape, backend)

        """
        self.requires_grad = requires_grad

        # TODO: IS RETAIN_GRAD EVEN NEEDED? IDK YET.
        self.retain_grad = retain_grad

        If they are used at all, requires_grad and retain_grad should
        probably be tracks in self.values, e.g. self.values[REQUIRES_GRAD],
        where REQUIRES_GRAD is a Track object like VALUE or TYPE. That's
        because the current machinery to handle AbstractArray can already
        handle everything in values, whereas these extra fields will be
        ignored entirely, so they won't matter in equality comparisons,
        they will disappear whenever we try to copy an AbstractPyTorchTensor,
        etc.

        For the time being, I think I'd just axe them, until we have to
        actually do something with the information.
        """


APT = AbstractPyTorchTensor(ANYTHING, {SHAPE: ANYTHING})
