"""Tests for the public-tracking hub coordinator."""
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inpost.const import (
    CONF_COUNTRY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from custom_components.inpost.tracking.coordinator import InPostTrackingCoordinator


async def test_tracking_coordinator_builds_url_for_its_own_country(hass):
    """The tracking hub passes its entry's country into normalize_tracking_parcel."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="InPost (IT tracking)",
        unique_id="IT",
        data={CONF_COUNTRY: "IT"},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: "IT123"}]},
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = {
        "trackingNumber": "IT123",
        "status": "MMD.1001",
    }
    coordinator = InPostTrackingCoordinator(hass, client, entry)

    active = await coordinator._async_update_data()

    assert active[0]["url"] == "https://inpost.it/trova-il-tuo-pacco?number=IT123"


def _tracking_entry(codes: list[str]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="InPost (IT tracking)",
        unique_id="IT",
        data={CONF_COUNTRY: "IT"},
        options={
            CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes],
        },
    )


async def test_tracking_delivered_code_skipped_from_fetch(hass):
    """A delivered tracking code stops being fetched from the next cycle on."""
    entry = _tracking_entry(["IT-ACTIVE", "IT-DELIVERED"])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: (
        {"trackingNumber": "IT-ACTIVE", "status": "MMD.1001"}
        if code == "IT-ACTIVE"
        else {"trackingNumber": "IT-DELIVERED", "status": "EOL.1001"}
    )
    coordinator = InPostTrackingCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert client.async_get_parcel.call_count == 2
    assert coordinator.delivered_codes == {"IT-DELIVERED"}

    client.async_get_parcel.reset_mock()
    data = await coordinator._async_update_data()

    # Only the still-active code is fetched — the delivered one is skipped.
    client.async_get_parcel.assert_called_once_with("IT-ACTIVE")
    assert any(p["barcode"] == "IT-DELIVERED" for p in coordinator.delivered)
    assert data[0]["barcode"] == "IT-ACTIVE"


async def test_tracking_delivered_code_forgotten_when_untracked(hass):
    """Untracking a delivered code drops it from the skip set too."""
    entry = _tracking_entry(["IT-DELIVERED"])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = {
        "trackingNumber": "IT-DELIVERED",
        "status": "EOL.1001",
    }
    coordinator = InPostTrackingCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert coordinator.delivered_codes == {"IT-DELIVERED"}

    hass.config_entries.async_update_entry(entry, options={CONF_PARCELS: []})
    await coordinator._async_update_data()
    assert coordinator.delivered_codes == set()
