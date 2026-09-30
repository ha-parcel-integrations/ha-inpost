"""The pre-split import paths must keep resolving.

`api.py`, `coordinator.py` and `parcels.py` at package root became re-export
shims when the two sources were split into `account/` and `tracking/`.
Nothing in this repo imports them any more, so only a test notices if a rename
quietly breaks a user's automation or an external import.
"""
from custom_components.inpost import api, coordinator, parcels
from custom_components.inpost.account import client as account_client
from custom_components.inpost.account import coordinator as account_coordinator
from custom_components.inpost.account import parcels as account_parcels
from custom_components.inpost.tracking import client as tracking_client
from custom_components.inpost.tracking import coordinator as tracking_coordinator
from custom_components.inpost.tracking import parcels as tracking_parcels


def test_root_api_module_re_exports_both_clients():
    assert api.InPostApiClient is account_client.InPostApiClient
    assert api.InPostApiError is account_client.InPostApiError
    assert api.InPostAuthReauthRequired is account_client.InPostAuthReauthRequired
    assert api.async_send_sms_code is account_client.async_send_sms_code
    assert api.async_confirm_sms_code is account_client.async_confirm_sms_code
    assert api.InPostTrackingApiClient is tracking_client.InPostTrackingApiClient


def test_root_coordinator_module_re_exports_both_coordinators():
    assert coordinator.InPostCoordinator is account_coordinator.InPostCoordinator
    assert (
        coordinator.InPostTrackingCoordinator
        is tracking_coordinator.InPostTrackingCoordinator
    )


def test_root_parcels_module_re_exports_both_normalisers():
    for name in (
        "apply_delivered_filter",
        "build_history",
        "map_parcel_status",
        "normalize_parcel",
        "parse_iso",
        "sort_parcels_by_ts",
        "to_iso_timestamp",
    ):
        assert getattr(parcels, name) is getattr(account_parcels, name), name
    assert parcels.normalize_tracking_parcel is tracking_parcels.normalize_tracking_parcel
    assert parcels.tracking_hub_url is tracking_parcels.tracking_hub_url
