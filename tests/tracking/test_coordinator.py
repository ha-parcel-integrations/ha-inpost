"""Tests for the public-tracking hub coordinator."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inpost.const import (
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
    MID_INTERVAL_MINUTES,
    STAGGER_MINUTES,
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


async def test_failed_poll_drops_the_overnight_interval(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="InPost (IT tracking)",
        unique_id="IT",
        data={CONF_COUNTRY: "IT"},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: "IT123"}]},
    )
    entry.add_to_hass(hass)
    coordinator = InPostTrackingCoordinator(hass, AsyncMock(), entry)
    coordinator.update_interval = timedelta(hours=5, minutes=40)

    noon = datetime(2026, 1, 15, 12, tzinfo=UTC)
    with (
        patch.object(coordinator, "_async_poll", side_effect=TimeoutError),
        patch(
            "custom_components.inpost.account.coordinator.dt_util.now",
            return_value=noon,
        ),
        pytest.raises(TimeoutError),
    ):
        await coordinator._async_update_data()

    assert coordinator.update_interval is not None
    assert coordinator.update_interval <= timedelta(
        minutes=MID_INTERVAL_MINUTES + STAGGER_MINUTES
    )


async def test_a_parcel_the_delivered_list_leaves_out_still_fires_delivered(hass):
    """The retention option limits the delivered sensor, never the events."""
    entry = _tracking_entry(["IT-ONE", "IT-TWO"])
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 1,
        },
    )
    client = AsyncMock()
    coordinator = InPostTrackingCoordinator(hass, client, entry)
    delivered = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))

    client.async_get_parcel.side_effect = lambda code: {
        "trackingNumber": code,
        "status": "MMD.1001",
    }
    await coordinator._async_update_data()
    client.async_get_parcel.side_effect = lambda code: {
        "trackingNumber": code,
        "status": "EOL.1001",
    }
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert sorted(e.data["barcode"] for e in delivered) == ["IT-ONE", "IT-TWO"]
    assert len(coordinator.delivered) == 1
