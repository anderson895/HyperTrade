"""Trading strategies.

Concrete strategies are imported here so that registering them is a side effect of
importing the package — `available()` is then complete for the Settings dropdown.
"""

from .base import Strategy, available, create, register
from .volume_rejection import VolumeRejection

__all__ = ["Strategy", "VolumeRejection", "available", "create", "register"]
