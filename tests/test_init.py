"""Tests for InPost setup, unload, token persistence and reauth."""
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inpost.api import InPostApiError, InPostAuthReauthRequired
from custom_components.inpost.const import (
    CONF_AUTH_TOKEN,
    CONF_PHONE,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

from .payloads import ACTIVE_CODE, in_transit_sample, ready_sample

PHONE = "600123456"
GET = "custom_components.inpost.api.InPostApiClient.async_get_parcels"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=PHONE,
        unique_id=PHONE,
        data={CONF_PHONE: PHONE, CONF_AUTH_TOKEN: "acc", CONF_REFRESH_TOKEN: "ref"},
    )


async def test_setup_and_unload(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(GET, new=AsyncMock(return_value=[ready_sample()])):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    incoming = hass.states.get("sensor.inpost_600123456_incoming_parcels")
    assert incoming is not None
    assert incoming.state == "1"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_dead_session_starts_reauth(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    # The failed setup auto-starts a reauth flow, whose first step texts an SMS —
    # patch that so the flow does not reach the real network.
    with (
        patch(GET, new=AsyncMock(side_effect=InPostAuthReauthRequired("dead"))),
        patch(
            "custom_components.inpost.config_flow.async_send_sms_code",
            new=AsyncMock(),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert any(
            flow["context"]["source"] == "reauth"
            for flow in hass.config_entries.flow.async_progress()
        )


async def test_transient_error_retries(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(GET, new=AsyncMock(side_effect=InPostApiError("HTTP 503"))):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress()


async def test_rotated_tokens_are_persisted_into_the_entry(hass):
    """When the client refreshes mid-poll, the new pair must land in entry.data
    so a restart does not fall back to the stale refresh token."""
    entry = _entry()
    entry.add_to_hass(hass)

    async def _refresh_during_poll(self):
        # Simulate the client rotating tokens and invoking its callback.
        self._on_tokens_updated("acc-new", "ref-new")
        return [ready_sample()]

    with patch(GET, autospec=True, side_effect=_refresh_during_poll):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.data[CONF_AUTH_TOKEN] == "acc-new"
    assert entry.data[CONF_REFRESH_TOKEN] == "ref-new"


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=[ready_sample()])
    with patch(GET, new=mock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
        )

        other = "630400111111111111111111"
        mock.return_value = [in_transit_sample(other)]
        await entry.runtime_data.coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{other}"
        )
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
            )
            is None
        )
