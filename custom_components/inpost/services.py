"""Services for country-scoped, keyless InPost tracking hubs."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import CONF_COUNTRY, CONF_PARCELS, CONF_TRACKING_CODE, DOMAIN

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"
_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Required(CONF_COUNTRY, default="PL"): cv.string,
    }
)


def _entry(hass: HomeAssistant, country: str):
    """Resolve the matching public-tracking hub, never an account entry."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_COUNTRY) == country:
            return entry
    raise ServiceValidationError(f"InPost public tracking is not set up for {country}")


def async_setup_services(hass: HomeAssistant) -> None:
    """Register public-tracking services once for all country hubs."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        code = call.data[CONF_TRACKING_CODE].strip()
        if not code:
            raise ServiceValidationError("tracking_code must not be empty")
        entry = _entry(hass, call.data[CONF_COUNTRY].upper())
        parcels = [dict(item) for item in entry.options.get(CONF_PARCELS, [])]
        if any(item.get(CONF_TRACKING_CODE) == code for item in parcels):
            return
        parcels.append({CONF_TRACKING_CODE: code})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        entry = _entry(hass, call.data[CONF_COUNTRY].upper())
        code = call.data[CONF_TRACKING_CODE].strip()
        parcels = entry.options.get(CONF_PARCELS, [])
        kept = [item for item in parcels if item.get(CONF_TRACKING_CODE) != code]
        if len(kept) != len(parcels):
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_PARCELS: kept}
            )

    hass.services.async_register(DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_SCHEMA)


def async_unload_services(hass: HomeAssistant, unloading_entry: ConfigEntry) -> None:
    """Remove public-tracking services when its last hub is unloaded.

    ``unloading_entry`` is excluded from the remaining-hubs check: HA still
    lists it in ``async_entries()`` while its own ``async_unload_entry`` is
    running, so without the exclusion the last hub would always see itself
    and the services would never be cleaned up.
    """
    if any(
        entry.data.get(CONF_COUNTRY)
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.entry_id != unloading_entry.entry_id
    ):
        return
    for service in (SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
