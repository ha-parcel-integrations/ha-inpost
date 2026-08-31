"""InPost parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import InPostApiClient, InPostTrackingApiClient
from .const import CONF_AUTH_TOKEN, CONF_COUNTRY, CONF_REFRESH_TOKEN, PLATFORMS
from .coordinator import InPostCoordinator, InPostTrackingCoordinator
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


@dataclass
class InPostData:
    """Runtime data attached to an InPost config entry."""

    client: InPostApiClient | InPostTrackingApiClient
    coordinator: InPostCoordinator | InPostTrackingCoordinator


type InPostConfigEntry = ConfigEntry[InPostData]


async def async_setup_entry(hass: HomeAssistant, entry: InPostConfigEntry) -> bool:
    """Set up InPost from a config entry.

    Auth is header-based (a bearer-style token), not cookie-based, so the
    HA-managed shared session is fine — no per-entry cookie jar, and nothing to
    close on unload. The SMS login already happened in the config flow; here we
    only have the stored token pair.
    """
    # The fixed interval option was retired in 1.1.0. Remove any value left
    # on older entries so it cannot be mistaken for an active preference.
    if "refresh_interval" in entry.options:
        options = dict(entry.options)
        options.pop("refresh_interval")
        hass.config_entries.async_update_entry(entry, options=options)

    session = async_get_clientsession(hass)

    @callback
    def _persist_tokens(auth_token: str, refresh_token: str) -> None:
        """Write a rotated token pair back into the entry so it survives restart."""
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_AUTH_TOKEN: auth_token,
                CONF_REFRESH_TOKEN: refresh_token,
            },
        )

    if CONF_COUNTRY in entry.data:
        # Public tracking hubs have no credentials and can never reauthenticate.
        client = InPostTrackingApiClient(session)
        coordinator = InPostTrackingCoordinator(hass, client, entry)
    else:
        client = InPostApiClient(
            session,
            entry.data[CONF_AUTH_TOKEN],
            entry.data[CONF_REFRESH_TOKEN],
            on_tokens_updated=_persist_tokens,
        )
        coordinator = InPostCoordinator(hass, client, entry)

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to catch
    # cleanly; doing the first refresh here lets a transient failure fail the
    # whole entry so HA retries with backoff, and a dead session raise
    # ConfigEntryAuthFailed so HA starts the reauth flow.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = InPostData(client=client, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if CONF_COUNTRY in entry.data:
        entry.async_on_unload(entry.add_update_listener(_async_tracking_options_updated))
        async_setup_services(hass)

    # No entry.add_update_listener: the options flow calls async_schedule_reload
    # itself. Combining an update listener with a reload-on-update flow is
    # deprecated and becomes an error in HA 2026.12+.
    return True


async def _async_tracking_options_updated(
    hass: HomeAssistant, entry: InPostConfigEntry
) -> None:
    """Apply tracking-code changes immediately without an entry reload."""
    await entry.runtime_data.coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: InPostConfigEntry) -> bool:
    """Unload an InPost config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    if CONF_COUNTRY in entry.data:
        async_unload_services(hass, entry)
    return True
