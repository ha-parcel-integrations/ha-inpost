"""Compatibility imports for the two InPost coordinators."""
from .account.coordinator import InPostCoordinator
from .tracking.coordinator import InPostTrackingCoordinator

__all__ = ["InPostCoordinator", "InPostTrackingCoordinator"]
