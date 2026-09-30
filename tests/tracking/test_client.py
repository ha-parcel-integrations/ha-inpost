"""Tests for the keyless public-tracking client."""
import pytest

from custom_components.inpost.account.client import InPostApiError
from custom_components.inpost.tracking.client import InPostTrackingApiClient

from ..sessions import fake_session as _session


async def test_public_tracking_client_requests_keyless_endpoint():
    session = _session((200, {"trackingNumber": "TEST-1", "status": "new"}))
    parcel = await InPostTrackingApiClient(session).async_get_parcel("TEST-1")
    assert parcel["trackingNumber"] == "TEST-1"
    assert session.get.call_args.kwargs["params"] == {"language": "en"}


async def test_public_tracking_semantic_500_is_retryable_api_error():
    session = _session((200, {"status": 500}))
    with pytest.raises(InPostApiError) as err:
        await InPostTrackingApiClient(session).async_get_parcel("TEST-1")
    assert err.value.status_code == 500
