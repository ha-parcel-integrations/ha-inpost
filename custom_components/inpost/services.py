"""Services for InPost: public-tracking hubs and the contents text.

`inpost.track_parcel` / `inpost.untrack_parcel` belong to the country-scoped,
keyless tracking hubs. `inpost.update_contents` sets or clears the text
describing what is in a parcel, tracked or not, on the hub(s) or account(s)
that list the code.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import (
    CONF_CONFIG_ENTRY_ID,
    CONF_CONTENTS,
    CONF_COUNTRY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from .contents import ContentsStore, normalize_code

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"
SERVICE_UPDATE_CONTENTS = "update_contents"
_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Required(CONF_COUNTRY, default="PL"): cv.string,
    }
)
_TRACK_SCHEMA = _SCHEMA.extend({vol.Optional(CONF_CONTENTS): cv.string})
_UPDATE_CONTENTS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
        vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
    }
)


def _entry(hass: HomeAssistant, country: str):
    """Resolve the matching public-tracking hub, never an account entry."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_COUNTRY) == country:
            return entry
    raise ServiceValidationError(f"InPost public tracking is not set up for {country}")


def _loaded_coordinator(entry):
    """Return the coordinator of a set-up entry, or raise when it is not loaded."""
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError("InPost is not loaded")
    return entry.runtime_data.coordinator


def _contents_store(entry) -> ContentsStore | None:
    """Return the entry's contents store, or ``None`` when it is not loaded."""
    if entry.state is not ConfigEntryState.LOADED:
        return None
    return entry.runtime_data.coordinator.contents_store


def async_setup_services(hass: HomeAssistant, *, hub: bool = True) -> None:
    """Register the InPost services (idempotent).

    ``update_contents`` serves every entry; ``track_parcel`` and
    ``untrack_parcel`` belong to the tracking hubs, so an account entry passes
    ``hub=False``.
    """

    async def _track(call: ServiceCall) -> None:
        code = call.data[CONF_TRACKING_CODE].strip()
        if not code:
            raise ServiceValidationError("tracking_code must not be empty")
        entry = _entry(hass, call.data[CONF_COUNTRY].upper())

        has_contents = CONF_CONTENTS in call.data
        coordinator = _loaded_coordinator(entry) if has_contents else None

        parcels = [dict(item) for item in entry.options.get(CONF_PARCELS, [])]
        if any(item.get(CONF_TRACKING_CODE) == code for item in parcels):
            # Already tracked: only a changed contents value does anything.
            if coordinator is not None and coordinator.contents_store.async_set(
                code, call.data[CONF_CONTENTS]
            ):
                coordinator.async_reapply_contents()
            return
        if coordinator is not None:
            # Stored before the options write, whose listener refreshes.
            coordinator.contents_store.async_set(code, call.data[CONF_CONTENTS])
        parcels.append({CONF_TRACKING_CODE: code})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        entry = _entry(hass, call.data[CONF_COUNTRY].upper())
        code = call.data[CONF_TRACKING_CODE].strip()
        if (store := _contents_store(entry)) is not None:
            store.async_set(code, None)
        parcels = entry.options.get(CONF_PARCELS, [])
        kept = [item for item in parcels if item.get(CONF_TRACKING_CODE) != code]
        if len(kept) != len(parcels):
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_PARCELS: kept}
            )

    async def _update_contents(call: ServiceCall) -> None:
        code = normalize_code(call.data[CONF_TRACKING_CODE])
        if not code:
            raise ServiceValidationError("A tracking code is required")
        loaded = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if not loaded:
            raise ServiceValidationError("InPost is not set up")
        if entry_id := call.data.get(CONF_CONFIG_ENTRY_ID):
            targets = [entry for entry in loaded if entry.entry_id == entry_id]
            if not targets:
                raise ServiceValidationError(
                    f"'{entry_id}' is not a loaded InPost entry"
                )
        elif len(loaded) == 1:
            targets = loaded
        else:
            # A code can sit in two entries, so every entry that lists it gets
            # the value; guessing an entry would write to the wrong one.
            targets = [
                entry
                for entry in loaded
                if code in entry.runtime_data.coordinator.known_codes
            ]
            if not targets:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="update_contents_entry_required",
                )
        for entry in targets:
            coordinator = entry.runtime_data.coordinator
            if coordinator.contents_store.async_set(code, call.data.get(CONF_CONTENTS)):
                coordinator.async_reapply_contents()

    if hub and not hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        hass.services.async_register(
            DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
        )
        hass.services.async_register(
            DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, SERVICE_UPDATE_CONTENTS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_UPDATE_CONTENTS,
            _update_contents,
            schema=_UPDATE_CONTENTS_SCHEMA,
        )


def async_unload_services(hass: HomeAssistant, unloading_entry: ConfigEntry) -> None:
    """Remove the services their entries no longer need.

    ``unloading_entry`` is excluded from the remaining-entries checks: HA still
    lists it in ``async_entries()`` while its own ``async_unload_entry`` is
    running, so without the exclusion the last entry would always see itself
    and the services would never be cleaned up. The hub services go with the
    last tracking hub; ``update_contents`` stays while any entry is loaded.
    """
    others = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.entry_id != unloading_entry.entry_id
    ]
    services = []
    if not any(entry.data.get(CONF_COUNTRY) for entry in others):
        services += [SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL]
    if not any(entry.state is ConfigEntryState.LOADED for entry in others):
        services.append(SERVICE_UPDATE_CONTENTS)
    for service in services:
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
