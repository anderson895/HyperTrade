"""Trading strategies.

Concrete strategies are imported here so that registering them is a side effect of
importing the package — `available()` is then complete for the Settings dropdown.
"""

from .base import Strategy, available, create, register
from .trend_following import TrendFollowing

__all__ = ["Strategy", "TrendFollowing", "available", "create", "register"]
