"""Compatibility imports for the two InPost clients.

New code imports from :mod:`inpost.account.client` and
:mod:`inpost.tracking.client`; this module preserves the pre-split public
import path for custom automations and older tests.
"""
from .account.client import (
    InPostApiClient,
    InPostApiError,
    InPostAuthReauthRequired,
)
from .tracking.client import InPostTrackingApiClient

__all__ = [
    "InPostApiClient",
    "InPostApiError",
    "InPostAuthReauthRequired",
    "InPostTrackingApiClient",
]
