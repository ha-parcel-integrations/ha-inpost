"""Config flow for the InPost parcel tracker integration.

InPost has no password. You log in the way the mobile app does: give a phone
number, InPost texts a one-time code, you type it back, and that exchange yields
the token pair the integration stores. So both the initial setup and reauth are
**two-step** — phone, then SMS code — which is the one genuinely new shape in
this suite's config flows.
"""
from __future__ import annotations

import logging
import re
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

from .api import (
    InPostApiError,
    async_confirm_sms_code,
    async_send_sms_code,
)
from .const import (
    CONF_AUTH_TOKEN,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PHONE,
    CONF_REFRESH_INTERVAL,
    CONF_REFRESH_TOKEN,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    REFRESH_INTERVAL_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

_PHONE_SCHEMA = vol.Schema({vol.Required(CONF_PHONE): str})
_CODE_SCHEMA = vol.Schema({vol.Required("sms_code"): str})


def normalize_phone(value: str) -> str:
    """Return the bare 9-digit Polish national number.

    Accepts whatever the user pastes — ``+48 600 123 456``, ``0048600123456``,
    ``600-123-456`` — strips everything but digits, and drops a leading ``48``
    country code so the value matches what InPost's API expects.
    """
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("00"):
        # International 00 prefix, e.g. 0048600123456.
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("48"):
        digits = digits[2:]
    return digits


def valid_phone(value: str) -> bool:
    """Whether ``value`` looks like a Polish mobile number (9 digits)."""
    return bool(re.fullmatch(r"\d{9}", value))


def _interval_selector() -> selector.SelectSelector:
    """Return the refresh-interval dropdown selector (options translated via strings)."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[str(minutes) for minutes in REFRESH_INTERVAL_OPTIONS],
            translation_key=CONF_REFRESH_INTERVAL,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


class InPostConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the InPost integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Carry the phone number between the two SMS steps."""
        self._phone: str = ""

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
        """Step one: ask for the phone number and text an SMS code to it."""
        errors: dict[str, str] = {}

        if user_input is not None:
            phone = normalize_phone(user_input[CONF_PHONE])
            if not valid_phone(phone):
                errors["base"] = "invalid_phone"
            else:
                await self.async_set_unique_id(phone)
                self._abort_if_unique_id_configured()
                try:
                    await async_send_sms_code(
                        async_get_clientsession(self.hass), phone
                    )
                except (InPostApiError, aiohttp.ClientError) as err:
                    # The endpoint and request shape are confirmed working, so a
                    # failure here is almost always transport (the HA host can't
                    # reach InPost) or a transient HTTP error. Log the actual
                    # cause so it is not hidden behind the generic form message.
                    _LOGGER.warning("InPost could not send the SMS code: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    self._phone = phone
                    return await self.async_step_sms()

        return self.async_show_form(
            step_id="user", data_schema=_PHONE_SCHEMA, errors=errors
        )

    async def async_step_sms(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step two: exchange the typed SMS code for the token pair."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                auth_token, refresh_token = await async_confirm_sms_code(
                    async_get_clientsession(self.hass),
                    self._phone,
                    user_input["sms_code"].strip(),
                )
            except (InPostApiError, aiohttp.ClientError) as err:
                _LOGGER.warning("InPost could not confirm the SMS code: %s", err)
                errors["base"] = "invalid_auth"
            else:
                return self.async_create_entry(
                    title=self._phone,
                    data={
                        CONF_PHONE: self._phone,
                        CONF_AUTH_TOKEN: auth_token,
                        CONF_REFRESH_TOKEN: refresh_token,
                    },
                    options={
                        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                        CONF_REFRESH_INTERVAL: DEFAULT_REFRESH_INTERVAL,
                        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                    },
                )

        return self.async_show_form(
            step_id="sms",
            data_schema=_CODE_SCHEMA,
            errors=errors,
            description_placeholders={"phone": self._phone},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth: the token pair expired, so log in by SMS again."""
        self._phone = entry_data[CONF_PHONE]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-send an SMS to the stored number, then confirm the code.

        The phone is fixed to the entry's, so reauth cannot silently rebind the
        entry to a different account.
        """
        errors: dict[str, str] = {}

        if user_input is None:
            # First entry into the step: text a fresh code before showing the
            # code field.
            try:
                await async_send_sms_code(
                    async_get_clientsession(self.hass), self._phone
                )
            except (InPostApiError, aiohttp.ClientError) as err:
                _LOGGER.warning("InPost could not send the SMS code: %s", err)
                errors["base"] = "cannot_connect"
        else:
            try:
                auth_token, refresh_token = await async_confirm_sms_code(
                    async_get_clientsession(self.hass),
                    self._phone,
                    user_input["sms_code"].strip(),
                )
            except (InPostApiError, aiohttp.ClientError) as err:
                _LOGGER.warning("InPost could not confirm the SMS code: %s", err)
                errors["base"] = "invalid_auth"
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_AUTH_TOKEN: auth_token,
                        CONF_REFRESH_TOKEN: refresh_token,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_CODE_SCHEMA,
            errors=errors,
            description_placeholders={"phone": self._phone},
        )


class InPostOptionsFlowHandler(OptionsFlow):
    """Manage delivered retention, history and polling in one sectioned form."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the single sectioned options form."""
        if user_input is not None:
            delivered = user_input["delivered"]
            history = user_input["history"]
            polling = user_input["polling"]
            # Reload so a changed interval takes effect immediately. No update
            # listener is registered — combining the two is deprecated.
            self.hass.config_entries.async_schedule_reload(
                self.config_entry.entry_id
            )
            return self.async_create_entry(
                title="",
                data={
                    CONF_DELIVERED_FILTER_TYPE: delivered[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        delivered[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(history[CONF_INCLUDE_HISTORY]),
                    CONF_REFRESH_INTERVAL: int(polling[CONF_REFRESH_INTERVAL]),
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
                vol.Required("polling"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_REFRESH_INTERVAL,
                                # str(): selector option values are strings, so
                                # a stored int default trips "expected str".
                                default=str(
                                    current.get(
                                        CONF_REFRESH_INTERVAL,
                                        DEFAULT_REFRESH_INTERVAL,
                                    )
                                ),
                            ): _interval_selector(),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
