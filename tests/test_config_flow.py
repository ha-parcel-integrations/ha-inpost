"""Tests for the InPost config and options flow — the two-step SMS login."""
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_USER
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inpost.api import InPostApiError
from custom_components.inpost.config_flow import normalize_phone, valid_phone
from custom_components.inpost.const import (
    CONF_AUTH_TOKEN,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PHONE,
    CONF_REFRESH_INTERVAL,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

PHONE = "600123456"
SEND = "custom_components.inpost.config_flow.async_send_sms_code"
CONFIRM = "custom_components.inpost.config_flow.async_confirm_sms_code"


def test_normalize_phone_strips_and_drops_country_code():
    assert normalize_phone("+48 600 123 456") == "600123456"
    assert normalize_phone("0048600123456") == "600123456"
    assert normalize_phone("600-123-456") == "600123456"
    assert normalize_phone("") == ""


def test_valid_phone_wants_nine_digits():
    assert valid_phone("600123456")
    assert not valid_phone("12345")
    assert not valid_phone("6001234567")


# ---------------------------------------------------------------------------
# initial setup
# ---------------------------------------------------------------------------


async def _start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def test_full_sms_flow_creates_entry(hass):
    result = await _start(hass)
    assert result["step_id"] == "user"

    with patch(SEND, new=AsyncMock()) as send:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PHONE: "+48 600 123 456"}
        )
    assert result["step_id"] == "sms"
    send.assert_awaited_once()  # code was texted

    with patch(CONFIRM, new=AsyncMock(return_value=("acc-1", "ref-1"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "1234"}
        )

    assert result["type"] == "create_entry"
    assert result["title"] == PHONE
    assert result["data"] == {
        CONF_PHONE: PHONE,
        CONF_AUTH_TOKEN: "acc-1",
        CONF_REFRESH_TOKEN: "ref-1",
    }
    assert result["options"][CONF_REFRESH_INTERVAL] == 30


async def test_invalid_phone_is_rejected(hass):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PHONE: "12345"}
    )
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_phone"}


async def test_send_sms_failure_surfaces_cannot_connect(hass):
    result = await _start(hass)
    with patch(SEND, new=AsyncMock(side_effect=aiohttp.ClientError("boom"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PHONE: PHONE}
        )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_wrong_code_surfaces_invalid_auth(hass):
    result = await _start(hass)
    with patch(SEND, new=AsyncMock()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PHONE: PHONE}
        )
    with patch(CONFIRM, new=AsyncMock(side_effect=InPostApiError("HTTP 400"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "0000"}
        )
    assert result["step_id"] == "sms"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_duplicate_phone_aborts_before_texting(hass):
    MockConfigEntry(domain=DOMAIN, unique_id=PHONE, data={CONF_PHONE: PHONE}).add_to_hass(
        hass
    )
    result = await _start(hass)
    with patch(SEND, new=AsyncMock()) as send:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PHONE: PHONE}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    send.assert_not_awaited()


# ---------------------------------------------------------------------------
# reauth
# ---------------------------------------------------------------------------


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=PHONE,
        unique_id=PHONE,
        data={CONF_PHONE: PHONE, CONF_AUTH_TOKEN: "old", CONF_REFRESH_TOKEN: "old"},
        options={
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_INCLUDE_HISTORY: False,
            CONF_REFRESH_INTERVAL: 30,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_reauth_texts_a_code_then_updates_tokens(hass):
    entry = _entry(hass)

    with patch(SEND, new=AsyncMock()) as send:
        result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    send.assert_awaited_once()

    with patch(CONFIRM, new=AsyncMock(return_value=("acc-new", "ref-new"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "4321"}
        )
        await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_AUTH_TOKEN] == "acc-new"
    assert entry.data[CONF_REFRESH_TOKEN] == "ref-new"


async def test_reauth_wrong_code_surfaces_invalid_auth(hass):
    entry = _entry(hass)
    with patch(SEND, new=AsyncMock()):
        result = await entry.start_reauth_flow(hass)
    with patch(CONFIRM, new=AsyncMock(side_effect=InPostApiError("HTTP 400"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "0000"}
        )
    assert result["errors"] == {"base": "invalid_auth"}


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------


async def test_options_flow_saves_and_reloads(hass):
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "delivered": {
                    CONF_DELIVERED_FILTER_TYPE: "parcels",
                    CONF_DELIVERED_FILTER_AMOUNT: 5,
                },
                "history": {CONF_INCLUDE_HISTORY: True},
                "polling": {CONF_REFRESH_INTERVAL: "60"},
            },
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_REFRESH_INTERVAL] == 60
    reload.assert_called_once_with(entry.entry_id)
