"""Calendar platform for the InPost parcel tracker integration."""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import InPostConfigEntry
from .const import DOMAIN
from .device import ATTRIBUTION, build_device_info
from .coordinator import InPostCoordinator
from .parcels import parse_iso

PARALLEL_UPDATES = 0

_DEFAULT_EVENT_DURATION = timedelta(hours=1)



async def async_setup_entry(
    hass: HomeAssistant,
    entry: InPostConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the InPost deliveries calendar from a config entry."""
    async_add_entities([InPostDeliveriesCalendar(entry.runtime_data.coordinator, entry)])


class InPostDeliveriesCalendar(CoordinatorEntity[InPostCoordinator], CalendarEntity):
    """A read-only calendar of expected InPost deliveries.

    Each active tracked parcel with a known delivery moment becomes an event.
    No extra API calls — a pure view over coordinator data — so it is enabled
    by default and can be turned off per entity if unwanted.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "deliveries"
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: InPostCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_deliveries"
        self._attr_device_info = build_device_info(entry)

    def _events(self) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for parcel in self.coordinator.data or []:
            start = parse_iso(parcel.get("planned_from"))
            if start is None:
                continue
            end = parse_iso(parcel.get("planned_to"))
            if end is None or end <= start:
                end = start + _DEFAULT_EVENT_DURATION

            barcode = parcel.get("barcode") or ""
            sender = parcel.get("sender")
            summary = sender or (f"Parcel {barcode}" if barcode else "InPost parcel")
            description_parts = [
                f"Barcode: {barcode}" if barcode else None,
                f"Status: {parcel.get('status')}" if parcel.get("status") else None,
                parcel.get("url"),
            ]
            description = "\n".join(p for p in description_parts if p)
            location = parcel.get("pickup_point") if parcel.get("pickup") else None

            events.append(
                CalendarEvent(
                    start=start,
                    end=end,
                    summary=summary,
                    description=description or None,
                    location=location,
                    uid=barcode or None,
                )
            )
        return events

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        upcoming = [event for event in self._events() if event.end > now]
        return min(upcoming, key=lambda event: event.start) if upcoming else None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        return [
            event
            for event in self._events()
            if event.start < end_date and event.end > start_date
        ]
