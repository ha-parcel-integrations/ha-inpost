"""Diagnostics support for the InPost parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import InPostConfigEntry

# Diagnostics are pasted into public issues, so redact anything that identifies
# a person, an address, a specific parcel, or (worst of all) the codes that open
# a locker. Over-redacting is cheap; under-redacting leaks a home address — or a
# working openCode — into a GitHub thread.
TO_REDACT = {
    # canonical fields we publish ourselves
    "barcode",
    "sender",
    "receiver",
    "url",
    "pickup_point",
    # InPost payload fields
    "shipmentNumber",
    "name",
    "phoneNumber",
    "courierPhoneNumber",
    "email",
    "pickUpPoint",
    "address",
    "addressDetails",
    # locker access — a live openCode/qrCode is a physical-security leak
    "openCode",
    "qrCode",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: InPostConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the InPost config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
