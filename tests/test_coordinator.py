"""Tests for the InPost coordinator: fetching, splitting and events."""
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inpost.api import InPostApiError, InPostAuthReauthRequired
from custom_components.inpost.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    ParcelStatus,
)
from custom_components.inpost.coordinator import InPostCoordinator

from .payloads import (
    ACTIVE_CODE,
    delivered_sample,
    in_transit_sample,
    ready_sample,
)

PHONE = "600123456"
OTHER_CODE = "630400000000000000000000"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=PHONE,
        unique_id=PHONE,
        data={"phone": PHONE, "auth_token": "acc", "refresh_token": "ref"},
        # Keep-most-recent-100 so the retention filter never trims the sample.
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )


def _coordinator(hass, client) -> InPostCoordinator:
    entry = _entry()
    entry.add_to_hass(hass)
    return InPostCoordinator(hass, client, entry)


def _client(*parcels) -> AsyncMock:
    client = AsyncMock()
    client.async_get_parcels.return_value = list(parcels)
    return client


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


async def test_update_splits_active_and_delivered(hass):
    coordinator = _coordinator(hass, _client(ready_sample(), delivered_sample()))
    data = await coordinator._async_update_data()

    assert [p["barcode"] for p in data] == [ACTIVE_CODE]
    assert len(coordinator.delivered) == 1
    assert coordinator.last_success_time is not None


async def test_empty_account(hass):
    coordinator = _coordinator(hass, _client())
    assert await coordinator._async_update_data() == []


async def test_dead_session_triggers_reauth(hass):
    """A refresh that failed inside the client surfaces as reauth, not retry."""
    client = AsyncMock()
    client.async_get_parcels.side_effect = InPostAuthReauthRequired("dead")
    coordinator = _coordinator(hass, client)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_transient_error_is_not_reauth(hass):
    """A plain API error must propagate for UpdateFailed backoff, not reauth."""
    client = AsyncMock()
    client.async_get_parcels.side_effect = InPostApiError("HTTP 503")
    coordinator = _coordinator(hass, client)
    with pytest.raises(InPostApiError):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def test_first_refresh_fires_nothing(hass):
    coordinator = _coordinator(hass, _client(ready_sample()))
    fired = []
    for suffix in (
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
    ):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert fired == []


async def test_status_change_fires_status_changed(hass):
    client = _client(in_transit_sample())
    coordinator = _coordinator(hass, client)
    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    await coordinator._async_update_data()  # first: suppressed
    client.async_get_parcels.return_value = [ready_sample()]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.IN_TRANSIT
    assert events[0].data["new_status"] == ParcelStatus.AT_PICKUP_POINT


async def test_delivery_fires_delivered_not_status_changed(hass):
    client = _client(ready_sample(ACTIVE_CODE))
    coordinator = _coordinator(hass, client)
    delivered, changed = [], []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e)
    )

    await coordinator._async_update_data()
    client.async_get_parcels.return_value = [delivered_sample(ACTIVE_CODE)]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["status"] == ParcelStatus.DELIVERED


async def test_no_events_for_parcel_first_seen_delivered(hass):
    client = _client(ready_sample())
    coordinator = _coordinator(hass, client)
    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: fired.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: fired.append(e))

    await coordinator._async_update_data()
    client.async_get_parcels.return_value = [ready_sample(), delivered_sample()]
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert fired == []


async def test_registered_fires_for_a_new_parcel(hass):
    client = _client(ready_sample())
    coordinator = _coordinator(hass, client)
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    await coordinator._async_update_data()
    client.async_get_parcels.return_value = [
        ready_sample(),
        in_transit_sample(OTHER_CODE),
    ]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["barcode"] == OTHER_CODE


async def test_delivery_time_event_never_fires(hass):
    """InPost exposes no ETA, so this event has nothing to fire on."""
    client = _client(in_transit_sample())
    coordinator = _coordinator(hass, client)
    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    await coordinator._async_update_data()
    client.async_get_parcels.return_value = [ready_sample()]
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert events == []
