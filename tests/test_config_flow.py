"""Tests for the InPost config and options flow — the pasted-callback sign-in."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import aiohttp
from homeassistant.config_entries import SOURCE_USER
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inpost.account.client import (
    InPostApiError,
    InPostAuthReauthRequired,
)
from custom_components.inpost.const import (
    CONF_AUTH_METHOD,
    CONF_AUTH_TOKEN,
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_MARKET,
    CONF_PHONE,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    TRACKING_COUNTRIES,
)

from .tokens import make_jwt

PHONE = "600123456"
EXCHANGE = "custom_components.inpost.config_flow.async_exchange_code"
CALLBACK = "https://account.inpost-group.com/callback?code=the-code&state={state}"


def _tokens(
    market: str = "PL", phone: str | None = PHONE, prefix: str = "+48"
) -> tuple[str, str]:
    claims = {"market": market, "phone_prefix": prefix}
    if phone is not None:
        claims["phone"] = phone
    return make_jwt(claims), "ref-1"


def _state(result) -> str:
    url = result["description_placeholders"]["authorize_url"]
    return parse_qs(urlparse(url).query)["state"][0]


# ---------------------------------------------------------------------------
# initial setup
# ---------------------------------------------------------------------------


async def _start(hass, country: str = "pl"):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    assert result["step_id"] == "account"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: country}
    )


async def _paste(hass, result, exchange: AsyncMock, *, state: str | None = None):
    with patch(EXCHANGE, new=exchange):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"callback_url": CALLBACK.format(state=state or _state(result))},
        )


async def test_sign_in_creates_an_entry_keyed_on_the_token_phone(hass):
    result = await _start(hass)
    assert result["step_id"] == "sign_in"
    assert result["description_placeholders"]["authorize_url"].startswith(
        "https://account.inpost-group.com/oauth2/authorize?"
    )

    tokens = _tokens()
    exchange = AsyncMock(return_value=tokens)
    result = await _paste(hass, result, exchange)

    assert result["type"] == "create_entry"
    assert result["title"] == PHONE
    assert result["result"].unique_id == PHONE
    assert result["data"] == {
        CONF_PHONE: PHONE,
        CONF_MARKET: "PL",
        CONF_AUTH_METHOD: "sso",
        CONF_AUTH_TOKEN: tokens[0],
        CONF_REFRESH_TOKEN: "ref-1",
    }
    assert exchange.await_args.args[1] == "the-code"


async def test_retry_keeps_the_same_sign_in_link(hass):
    result = await _start(hass)
    first_url = result["description_placeholders"]["authorize_url"]
    result = await _paste(hass, result, AsyncMock(), state="wrong")
    assert result["errors"] == {"base": "invalid_redirect"}
    assert result["description_placeholders"]["authorize_url"] == first_url


async def test_a_url_from_elsewhere_is_never_exchanged(hass):
    result = await _start(hass)
    exchange = AsyncMock()
    with patch(EXCHANGE, new=exchange):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"callback_url": "https://evil.example/callback?code=x&state=y"},
        )
    assert result["errors"] == {"base": "invalid_redirect"}
    exchange.assert_not_awaited()


async def test_callback_without_a_code_is_rejected(hass):
    result = await _start(hass)
    exchange = AsyncMock()
    with patch(EXCHANGE, new=exchange):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"callback_url": f"https://account.inpost-group.com/callback?state={_state(result)}"},
        )
    assert result["errors"] == {"base": "invalid_redirect"}
    exchange.assert_not_awaited()


async def test_rejected_code_surfaces_invalid_auth(hass):
    result = await _start(hass)
    result = await _paste(hass, result, AsyncMock(side_effect=InPostAuthReauthRequired("400")))
    assert result["step_id"] == "sign_in"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_unreachable_sign_in_surfaces_cannot_connect(hass):
    for error in (InPostApiError("HTTP 502"), aiohttp.ClientError("boom")):
        result = await _start(hass)
        result = await _paste(hass, result, AsyncMock(side_effect=error))
        assert result["errors"] == {"base": "cannot_connect"}


async def test_an_account_from_another_country_than_chosen_is_refused(hass):
    """Usually a browser still signed in to another InPost account."""
    result = await _start(hass)
    result = await _paste(hass, result, AsyncMock(return_value=_tokens(market="IT")))
    assert result["errors"] == {"base": "wrong_market"}


async def test_italian_sign_in_creates_an_italian_entry(hass):
    result = await _start(hass, "it")
    query = parse_qs(urlparse(result["description_placeholders"]["authorize_url"]).query)
    assert query["supported_markets"] == ["IT"]

    tokens = _tokens(market="IT", phone="3201234567", prefix="+39")
    result = await _paste(hass, result, AsyncMock(return_value=tokens))

    assert result["type"] == "create_entry"
    assert result["title"] == "3201234567"
    assert result["result"].unique_id == "3201234567"
    assert result["data"][CONF_MARKET] == "IT"
    assert result["data"][CONF_PHONE] == "3201234567"


def test_account_countries_are_translated_in_every_locale():
    translations_dir = Path(__file__).parents[1] / "custom_components/inpost"
    paths = [translations_dir / "strings.json"] + sorted(
        (translations_dir / "translations").glob("*.json")
    )
    for path in paths:
        payload = json.loads(path.read_text())
        assert {"pl", "it"} <= set(payload["selector"]["country"]["options"]), path


async def test_a_token_without_a_phone_is_refused(hass):
    result = await _start(hass)
    result = await _paste(hass, result, AsyncMock(return_value=_tokens(phone=None)))
    assert result["errors"] == {"base": "invalid_auth"}


async def test_account_already_set_up_by_sms_aborts(hass):
    MockConfigEntry(domain=DOMAIN, unique_id=PHONE, data={CONF_PHONE: PHONE}).add_to_hass(
        hass
    )
    result = await _start(hass)
    result = await _paste(hass, result, AsyncMock(return_value=_tokens()))
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


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
    """An entry set up before the sign-in, still on the app's SMS-login tokens."""
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


async def test_reauth_moves_an_sms_entry_to_the_sign_in(hass):
    entry = _entry(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    tokens = _tokens()
    with patch(EXCHANGE, new=AsyncMock(return_value=tokens)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": CALLBACK.format(state=_state(result))}
        )
        await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == PHONE
    assert entry.data[CONF_AUTH_METHOD] == "sso"
    assert entry.data[CONF_AUTH_TOKEN] == tokens[0]
    assert entry.data[CONF_REFRESH_TOKEN] == "ref-1"


async def test_reauth_with_another_account_is_refused(hass):
    entry = _entry(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(EXCHANGE, new=AsyncMock(return_value=_tokens(phone="700000000"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": CALLBACK.format(state=_state(result))}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_AUTH_TOKEN] == "old"


async def test_reauth_rejected_code_surfaces_invalid_auth(hass):
    entry = _entry(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(EXCHANGE, new=AsyncMock(side_effect=InPostAuthReauthRequired("400"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": CALLBACK.format(state=_state(result))}
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


async def test_country_dropdowns_sort_by_translated_name(hass):
    for step in ("account", "tracking"):
        flow = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        form = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"next_step_id": step}
        )
        assert form["data_schema"].schema[CONF_COUNTRY].config["sort"] is True, step


def test_translations_contain_no_urls():
    """Hassfest rejects a URL in any string; links go through placeholders."""
    translations_dir = Path(__file__).parents[1] / "custom_components/inpost"
    paths = [translations_dir / "strings.json"] + sorted(
        (translations_dir / "translations").glob("*.json")
    )
    for path in paths:
        assert "://" not in path.read_text(), path
