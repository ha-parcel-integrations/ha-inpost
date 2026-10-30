"""Tests for the account coordinator: fetching, splitting and events."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inpost.account.client import (
    InPostApiError,
    InPostAuthReauthRequired,
)
from custom_components.inpost.account.coordinator import InPostCoordinator
from custom_components.inpost.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    MID_INTERVAL_MINUTES,
    STAGGER_MINUTES,
    ParcelStatus,
)
from tests.payloads import (
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


async def test_transient_error_becomes_update_failed_not_reauth(hass):
    """A plain API error must become UpdateFailed for HA's native retry — not
    reauth, and not a bare InPostApiError (DataUpdateCoordinator only treats
    UpdateFailed as a normal, retryable failure; anything else is logged as an
    unhandled crash)."""
    client = AsyncMock()
    client.async_get_parcels.side_effect = InPostApiError("HTTP 503")
    coordinator = _coordinator(hass, client)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_rate_limit_uses_retry_after_backoff(hass):
    client = AsyncMock()
    client.async_get_parcels.side_effect = InPostApiError(
        "HTTP 429", status_code=429, retry_after=120
    )
    coordinator = _coordinator(hass, client)
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after == 120


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
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e))

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


async def test_italian_account_uses_the_italian_normaliser(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="3201234567",
        unique_id="3201234567",
        data={
            "phone": "3201234567",
            "market": "IT",
            "auth_token": "a",
            "refresh_token": "r",
        },
    )
    entry.add_to_hass(hass)
    client = _client(
        {
            "primaryParcelNumber": "IT1",
            "events": [
                {
                    "eventCode": "LMD.1005",
                    "eventTime": "2026-09-30T10:00:00Z",
                    "status": "AWAITING_COLLECTION",
                }
            ],
        }
    )
    data = await InPostCoordinator(hass, client, entry)._async_update_data()
    assert data[0]["barcode"] == "IT1"
    assert data[0]["status"] == ParcelStatus.AT_PICKUP_POINT


async def test_failed_poll_drops_the_overnight_interval(hass):
    coordinator = _coordinator(hass, AsyncMock())
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
    client = _client(ready_sample(ACTIVE_CODE), ready_sample(OTHER_CODE))
    coordinator = _coordinator(hass, client)
    hass.config_entries.async_update_entry(
        coordinator.config_entry,
        options={**coordinator.config_entry.options, CONF_DELIVERED_FILTER_AMOUNT: 1},
    )
    delivered = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))

    await coordinator._async_update_data()
    client.async_get_parcels.return_value = [
        delivered_sample(ACTIVE_CODE),
        delivered_sample(OTHER_CODE),
    ]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert sorted(e.data["barcode"] for e in delivered) == sorted(
        [ACTIVE_CODE, OTHER_CODE]
    )
    assert len(coordinator.delivered) == 1
