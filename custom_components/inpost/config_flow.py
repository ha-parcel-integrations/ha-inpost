"""Config flow for the InPost parcel tracker integration.

InPost has no password. The account is signed in to on InPost's own sign-in
page (phone number, SMS code, a captcha), which Home Assistant cannot host: the
flow shows the sign-in link, the user completes it in their own browser and
pastes back the callback URL it lands on, and that code is exchanged for the
token pair the integration stores. Setup and reauth share the same step.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account.client import (
    InPostApiError,
    InPostAuthReauthRequired,
    async_exchange_code,
)
from .account.oauth import (
    SUPPORTED_MARKET,
    build_authorization_url,
    decode_token_claims,
    generate_nonce,
    generate_pkce,
    generate_state,
    is_valid_callback_url,
    parse_callback_url,
)
from .const import (
    AUTH_METHOD_SSO,
    CONF_AUTH_METHOD,
    CONF_AUTH_TOKEN,
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_PHONE,
    CONF_REFRESH_TOKEN,
    CONF_TRACKING_CODE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    TRACKING_COUNTRIES,
)

_LOGGER = logging.getLogger(__name__)

_CALLBACK_SCHEMA = vol.Schema({vol.Required("callback_url"): str})
_TRACKING_COUNTRY_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[country.lower() for country in TRACKING_COUNTRIES],
        translation_key=CONF_COUNTRY,
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)


class InPostConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the InPost integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise per-flow sign-in state — never persisted, never reused."""
        self._authorize_url: str | None = None
        self._code_verifier: str | None = None
        self._state: str | None = None
        self._phone: str | None = None
        self._tokens: tuple[str, str] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> InPostOptionsFlowHandler:
        """Return the options flow handler."""
        return InPostOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the independent account or public-tracking setup path."""
        return self.async_show_menu(
            step_id="user", menu_options=["account", "tracking"]
        )

    def _ensure_authorize_url(self) -> None:
        """Build the sign-in URL once per flow and keep it for the flow's lifetime.

        Regenerating the verifier/state on a retry would invalidate a link the
        user may already have opened.
        """
        if self._authorize_url is not None:
            return
        self._code_verifier, challenge = generate_pkce()
        self._state = generate_state()
        self._authorize_url = build_authorization_url(
            challenge, self._state, generate_nonce(), self.hass.config.language
        )

    async def _async_sign_in(self, callback_url: str) -> str | None:
        """Validate and exchange a pasted callback URL.

        Returns a form error, or ``None`` with ``self._tokens`` and
        ``self._phone`` set. Never logs the pasted value.
        """
        if not is_valid_callback_url(callback_url):
            return "invalid_redirect"
        code, state = parse_callback_url(callback_url)
        if not code or state != self._state:
            return "invalid_redirect"

        try:
            tokens = await async_exchange_code(
                async_get_clientsession(self.hass), code, self._code_verifier or ""
            )
        except InPostAuthReauthRequired:
            return "invalid_auth"
        except (InPostApiError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("InPost sign-in could not be completed: %s", err)
            return "cannot_connect"

        claims = decode_token_claims(tokens[0]) or {}
        if claims.get("market") != SUPPORTED_MARKET:
            return "market_not_supported"
        phone = claims.get("phone")
        if not (isinstance(phone, str) and phone.isdigit()):
            return "invalid_auth"
        self._phone = phone
        self._tokens = tokens
        return None

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the sign-in link and take the pasted-back callback URL."""
        self._ensure_authorize_url()
        errors: dict[str, str] = {}

        if user_input is not None:
            error = await self._async_sign_in(user_input["callback_url"])
            if error is not None:
                errors["base"] = error
            else:
                assert self._phone is not None and self._tokens is not None
                # Same bare national number the SMS-era entries are keyed on,
                # so an account set up both ways is caught as a duplicate.
                await self.async_set_unique_id(self._phone)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._phone,
                    data={
                        CONF_PHONE: self._phone,
                        CONF_AUTH_METHOD: AUTH_METHOD_SSO,
                        CONF_AUTH_TOKEN: self._tokens[0],
                        CONF_REFRESH_TOKEN: self._tokens[1],
                    },
                    options={
                        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                    },
                )

        return self.async_show_form(
            step_id="account",
            data_schema=_CALLBACK_SCHEMA,
            errors=errors,
            description_placeholders={"authorize_url": self._authorize_url or ""},
        )

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create a country-scoped hub for public tracking numbers."""
        if user_input is not None:
            country = user_input[CONF_COUNTRY].upper()
            await self.async_set_unique_id(f"{DOMAIN}_tracking_{country}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"InPost ({country} tracking)",
                data={CONF_COUNTRY: country},
                options={
                    CONF_PARCELS: [],
                    CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                    CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                    CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                },
            )
        return self.async_show_form(
            step_id="tracking",
            data_schema=vol.Schema(
                {vol.Required(CONF_COUNTRY): _TRACKING_COUNTRY_SELECTOR}
            ),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth: the stored token pair can no longer be refreshed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Repeat the sign-in and update the entry in place.

        An entry still on the app's SMS-login tokens moves to the sign-in here.
        """
        self._ensure_authorize_url()
        errors: dict[str, str] = {}

        if user_input is not None:
            error = await self._async_sign_in(user_input["callback_url"])
            if error is not None:
                errors["base"] = error
            else:
                assert self._phone is not None and self._tokens is not None
                # Signing in to a *different* account must not silently
                # rebind this entry to it.
                await self.async_set_unique_id(self._phone)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_AUTH_METHOD: AUTH_METHOD_SSO,
                        CONF_AUTH_TOKEN: self._tokens[0],
                        CONF_REFRESH_TOKEN: self._tokens[1],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_CALLBACK_SCHEMA,
            errors=errors,
            description_placeholders={"authorize_url": self._authorize_url or ""},
        )


class InPostOptionsFlowHandler(OptionsFlow):
    """Manage delivered retention, history and polling in one sectioned form."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Expose parcel management only for the keyless tracking model."""
        if CONF_COUNTRY in self.config_entry.data:
            return self.async_show_menu(
                step_id="init", menu_options=["parcels", "settings"]
            )
        return await self.async_step_settings(user_input)

    async def async_step_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace this public-tracking hub's explicit tracking-code list."""
        if user_input is not None:
            codes = list(
                dict.fromkeys(
                    (value or "").strip()
                    for value in user_input.get("tracking_codes", [])
                    if (value or "").strip()
                )
            )
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes],
                },
            )
        current_codes = [
            item.get(CONF_TRACKING_CODE, "")
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if isinstance(item, dict)
        ]
        return self.async_show_form(
            step_id="parcels",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {vol.Optional("tracking_codes"): selector.TextSelector(
                        selector.TextSelectorConfig(multiple=True)
                    )}
                ),
                {"tracking_codes": current_codes},
            ),
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the single sectioned options form."""
        if user_input is not None:
            delivered = user_input["delivered"]
            history = user_input["history"]
            return self.async_create_entry(
                title="",
                data={
                    CONF_DELIVERED_FILTER_TYPE: delivered[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        delivered[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(history[CONF_INCLUDE_HISTORY]),
                },
            )

        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required("delivered"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_DELIVERED_FILTER_TYPE,
                                default=current.get(
                                    CONF_DELIVERED_FILTER_TYPE,
                                    DEFAULT_DELIVERED_FILTER_TYPE,
                                ),
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=["days", "parcels"],
                                    translation_key=CONF_DELIVERED_FILTER_TYPE,
                                    mode=selector.SelectSelectorMode.LIST,
                                )
                            ),
                            vol.Required(
                                CONF_DELIVERED_FILTER_AMOUNT,
                                default=current.get(
                                    CONF_DELIVERED_FILTER_AMOUNT,
                                    DEFAULT_DELIVERED_FILTER_AMOUNT,
                                ),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=1,
                                    max=365,
                                    step=1,
                                    mode=selector.NumberSelectorMode.BOX,
                                )
                            ),
                        }
                    ),
                    {"collapsed": False},
                ),
                vol.Required("history"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_INCLUDE_HISTORY,
                                default=current.get(
                                    CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                                ),
                            ): selector.BooleanSelector(),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )

        return self.async_show_form(step_id="settings", data_schema=schema)
