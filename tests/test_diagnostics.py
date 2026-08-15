"""Tests for InPost diagnostics redaction."""
from unittest.mock import MagicMock

from custom_components.inpost.diagnostics import async_get_config_entry_diagnostics
from custom_components.inpost.parcels import normalize_parcel

from .payloads import ready_sample


async def test_diagnostics_redacts_pii_and_locker_codes(hass):
    """Diagnostics go into public issues — no name, address, tracking number or
    (worst of all) a working locker openCode may survive."""
    entry = MagicMock()
    entry.options = {"delivered_filter_type": "days"}
    entry.runtime_data.coordinator.data = [normalize_parcel(ready_sample())]
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {"incoming_active": 1, "delivered": 0}
    parcel = result["incoming"][0]
    assert parcel["barcode"] == "**REDACTED**"
    assert parcel["sender"] == "**REDACTED**"
    assert parcel["receiver"] == "**REDACTED**"
    assert parcel["pickup_point"] == "**REDACTED**"
    assert parcel["url"] == "**REDACTED**"
    # nested InPost payload fields
    assert parcel["raw"]["shipmentNumber"] == "**REDACTED**"
    assert parcel["raw"]["openCode"] == "**REDACTED**"
    assert parcel["raw"]["qrCode"] == "**REDACTED**"
    assert parcel["raw"]["pickUpPoint"] == "**REDACTED**"
    # non-identifying fields survive
    assert parcel["status"] == "at_pickup_point"


async def test_diagnostics_redacts_receiver_object_whole(hass):
    """A stranger's parcel landing in the account (2026-08-15) showed a
    ``receiver.email`` field the redaction list never named explicitly. It
    turns out not to leak — ``receiver`` itself is a redacted key, so
    ``async_redact_data`` blanks the whole nested object, email included. The
    ``email`` entry in ``TO_REDACT`` is still worth keeping as a direct guard
    in case a future payload ever surfaces an email outside ``receiver``/
    ``sender``."""
    entry = MagicMock()
    entry.options = {"delivered_filter_type": "days"}
    raw = ready_sample()
    raw["receiver"] = {
        "name": "Someone",
        "phoneNumber": "123456789",
        "email": "someone@example.com",
    }
    entry.runtime_data.coordinator.data = [normalize_parcel(raw)]
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["incoming"][0]["raw"]["receiver"] == "**REDACTED**"
