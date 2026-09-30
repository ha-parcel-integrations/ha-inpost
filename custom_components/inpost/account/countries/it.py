"""The Italian account inbox: InPost's group parcel backend.

An Italian account's sign-in token is refused by the Polish inbox and reads
its parcels from a separate, paged endpoint with its own shape. The sign-in
and the parcel list are live-confirmed; the per-parcel shape is modelled on
the InPost app and not yet seen with a real parcel, so every guess below
reports itself once when a payload disagrees.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ...const import HISTORY_MAX_EVENTS, TRACKING_URL_BY_COUNTRY, ParcelStatus
from ..parcels import NEW_ISSUE_URL, parse_iso, to_iso_timestamp

_LOGGER = logging.getLogger(__name__)

PARCELS_URL = "https://api-inmobile-pl.easypack24.net/global/cps/api/v1/parcels"
# Parcels addressed to the account. ``SENDER`` is the only other role.
PARCELS_ROLE = "RECEIVER"
# A page cap, so a cursor that never ends cannot keep a poll running forever.
MAX_PAGES = 10

# InPost publishes this vocabulary itself (its status catalogue), and it is
# the same eight codes in every market; only the display titles differ.
STATUS_MAP: dict[str, ParcelStatus] = {
    "at_the_origin": ParcelStatus.REGISTERED,
    "in_transit": ParcelStatus.IN_TRANSIT,
    "in_transit_last_mile": ParcelStatus.OUT_FOR_DELIVERY,
    "awaiting_collection": ParcelStatus.AT_PICKUP_POINT,
    "delivered": ParcelStatus.DELIVERED,
    "not_delivered": ParcelStatus.PROBLEM,
    "exception": ParcelStatus.PROBLEM,
    "return": ParcelStatus.RETURNING,
}

_KNOWN_PAYLOAD_KEYS = {
    "parcelId",
    "primaryParcelNumber",
    "events",
    "services",
    "sender",
    "receiver",
    "attributes",
    "pickUp",
    "dropOff",
    "actions",
}
_payload_shape_logged = False
_unmapped_statuses_logged: set[str] = set()


def _note_payload_shape(raw: dict) -> None:
    """One-shot: report an Italian parcel that does not look like the model."""
    global _payload_shape_logged
    if _payload_shape_logged:
        return
    extra = sorted(set(raw) - _KNOWN_PAYLOAD_KEYS)
    missing = sorted(
        key
        for key, expected in (("primaryParcelNumber", str), ("events", list))
        if not isinstance(raw.get(key), expected)
    )
    if not extra and not missing:
        return
    _payload_shape_logged = True
    _LOGGER.warning(
        "InPost Italy parcel differs from the shape we modelled (unexpected "
        "fields: %s; missing or different: %s). Italian parcels are new — "
        "please share a diagnostics file: %s",
        extra,
        missing,
        NEW_ISSUE_URL,
    )


def _map_status(code: Any) -> ParcelStatus | None:
    """Map one catalogue status code, reporting an unknown one once."""
    if not isinstance(code, str) or not code.strip():
        return None
    status = STATUS_MAP.get(code.strip().lower())
    if status is None and code not in _unmapped_statuses_logged:
        _unmapped_statuses_logged.add(code)
        _LOGGER.warning(
            "Unrecognised InPost Italy status — help us map it. Open an issue "
            "and paste this line: %s\n  status=%s → reported as 'unknown'",
            NEW_ISSUE_URL,
            code,
        )
    return status


def _name_of(party: Any) -> str | None:
    """Return the ``representativeName`` of a sender/receiver, or ``None``."""
    name = party.get("representativeName") if isinstance(party, dict) else None
    return name if isinstance(name, str) and name.strip() else None


def _pickup_point_name(pick_up: Any) -> str | None:
    """Return a label for the pickup point, whatever form ``point`` takes."""
    location = pick_up.get("location") if isinstance(pick_up, dict) else None
    point = location.get("point") if isinstance(location, dict) else None
    if isinstance(point, str) and point.strip():
        return point
    if isinstance(point, dict):
        for key in ("name", "id", "description"):
            value = point.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _ordered_events(raw: dict) -> list[tuple[datetime | None, dict]]:
    """Return the events oldest first; undated ones sort before the rest."""
    events = [event for event in raw.get("events") or [] if isinstance(event, dict)]
    dated = [(parse_iso(to_iso_timestamp(event.get("eventTime"))), event) for event in events]
    return sorted(
        dated, key=lambda item: (item[0] is not None, item[0] or datetime.min)
    )


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return the canonical parcel for one Italian inbox entry.

    The parcel carries no status of its own: its current status is its
    newest event's. ``weight`` and ``dimensions`` stay ``None`` until a real
    parcel shows their units.
    """
    _note_payload_shape(raw)
    tracking_code = raw.get("primaryParcelNumber")
    events = _ordered_events(raw)
    latest = events[-1][1] if events else {}

    status = _map_status(latest.get("status")) or ParcelStatus.UNKNOWN
    delivered = status is ParcelStatus.DELIVERED
    delivered_at = None
    if delivered:
        delivered_at = to_iso_timestamp(latest.get("eventTime"))

    history = None
    if include_history:
        history = [
            {
                "timestamp": to_iso_timestamp(event.get("eventTime")),
                "status": _map_status(event.get("status")),
                "raw_status": event.get("eventCode") or event.get("status"),
            }
            for _, event in events
        ][-HISTORY_MAX_EVENTS:]

    template = TRACKING_URL_BY_COUNTRY["IT"]
    return {
        "carrier": "InPost",
        "barcode": tracking_code,
        "sender": _name_of(raw.get("sender")),
        "receiver": _name_of(raw.get("receiver")),
        "status": status,
        "raw_status": latest.get("eventCode") or latest.get("status"),
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": None,
        "planned_to": None,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": _pickup_point_name(raw.get("pickUp")),
        "url": template.format(tracking_code=tracking_code) if tracking_code else None,
        "weight": None,
        "dimensions": None,
        "history": history,
        "raw": raw,
    }
