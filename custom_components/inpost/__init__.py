"""InPost parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import InPostApiClient
from .const import CONF_AUTH_TOKEN, CONF_REFRESH_TOKEN, PLATFORMS
from .coordinator import InPostCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class InPostData:
    """Runtime data attached to an InPost config entry."""

    client: InPostApiClient
    coordinator: InPostCoordinator


type InPostConfigEntry = ConfigEntry[InPostData]


async def async_setup_entry(hass: HomeAssistant, entry: InPostConfigEntry) -> bool:
    """Set up InPost from a config entry.

    Auth is header-based (a bearer-style token), not cookie-based, so the
    HA-managed shared session is fine — no per-entry cookie jar, and nothing to
    close on unload. The SMS login already happened in the config flow; here we
    only have the stored token pair.
    """
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

    # No entry.add_update_listener: the options flow calls async_schedule_reload
    # itself. Combining an update listener with a reload-on-update flow is
    # deprecated and becomes an error in HA 2026.12+.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: InPostConfigEntry) -> bool:
    """Unload an InPost config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
