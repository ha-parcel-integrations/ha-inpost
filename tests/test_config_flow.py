"""Tests for the InPost config and options flow — the two-step SMS login."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.config_entries import SOURCE_USER
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inpost.api import InPostApiError
from custom_components.inpost.config_flow import normalize_phone, valid_phone
from custom_components.inpost.const import (
    CONF_AUTH_TOKEN,
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PHONE,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    TRACKING_COUNTRIES,
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
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )


async def test_full_sms_flow_creates_entry(hass):
    result = await _start(hass)
    assert result["step_id"] == "account"

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


async def test_invalid_phone_is_rejected(hass):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PHONE: "12345"}
    )
    assert result["step_id"] == "account"
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


async def test_user_menu_routes_to_tracking_and_creates_country_hub(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"
    assert set(result["menu_options"]) == {"account", "tracking"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tracking"}
    )
    assert result["step_id"] == "tracking"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "it"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "InPost (IT tracking)"
    assert result["data"] == {CONF_COUNTRY: "IT"}


def test_country_translations_match_supported_tracking_countries():
    """Never offer a translated country that the public route cannot handle."""
    translations_dir = Path(__file__).parents[1] / "custom_components/inpost"
    expected = {country.lower() for country in TRACKING_COUNTRIES}
    paths = [translations_dir / "strings.json"] + sorted(
        (translations_dir / "translations").glob("*.json")
    )
    assert {path.stem for path in paths[1:]} == {"en", "es", "fr", "it", "nl", "pl", "pt"}
    for path in paths:
        payload = json.loads(path.read_text())
        assert set(payload["selector"]["country"]["options"]) == expected


def test_options_menu_labels_are_translated_in_every_locale():
    """A menu with no menu_options translation renders as blank buttons."""
    translations_dir = Path(__file__).parents[1] / "custom_components/inpost"
    paths = [translations_dir / "strings.json"] + sorted(
        (translations_dir / "translations").glob("*.json")
    )
    for path in paths:
        payload = json.loads(path.read_text())
        menu_options = payload["options"]["step"]["init"]["menu_options"]
        assert set(menu_options) == {"parcels", "settings"}, path
        assert all(label.strip() for label in menu_options.values()), path


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
            },
        )

    assert result["type"] == "create_entry"
    reload.assert_not_called()


async def test_tracking_hub_options_menu_leads_to_settings_step(hass):
    """The settings form must render under step_id 'settings', matching its
    own translation — not 'init', which is the menu's step_id."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="InPost (IT tracking)",
        unique_id="IT",
        data={CONF_COUNTRY: "IT"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["step_id"] == "init"
    assert set(result["menu_options"]) == {"parcels", "settings"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["step_id"] == "settings"
