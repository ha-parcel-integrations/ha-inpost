"""Normaliser for the keyless public-tracking source.

Structurally independent from the account inbox: its own payload, its own
status vocabulary, its own one-shot warnings. Only the suite-wide helpers
(timestamps, the issue link) are shared with :mod:`..account.parcels`.
"""
from __future__ import annotations

import logging
from datetime import datetime

from ..account.parcels import NEW_ISSUE_URL, parse_iso, to_iso_timestamp
from ..const import HISTORY_MAX_EVENTS, TRACKING_URL_BY_COUNTRY, ParcelStatus

_LOGGER = logging.getLogger(__name__)

# Public cross-border status vocabulary, live-confirmed on IT, PT and GB
# consignments on 2026-08-31.  These codes are deliberately separate from the
# authenticated Polish inbox vocabulary in ``account/parcels.py``: same carrier, different API.
TRACKING_STATUS_MAP: dict[str, str] = {
    # Creation and handover.
    "CRE.1001": ParcelStatus.REGISTERED,
    "FMD.1001": ParcelStatus.REGISTERED,
    "FMD.1002": ParcelStatus.IN_TRANSIT,
    # Logistics-centre movement.
    "MMD.1001": ParcelStatus.IN_TRANSIT,
    "MMD.1002": ParcelStatus.IN_TRANSIT,
    "MMD.1003": ParcelStatus.IN_TRANSIT,
    "MMD.1004": ParcelStatus.IN_TRANSIT,
    # Last-mile, redirects and collection.
    "LMD.1001": ParcelStatus.IN_TRANSIT,
    "LMD.1002": ParcelStatus.IN_TRANSIT,
    "LMD.3006": ParcelStatus.IN_TRANSIT,
    "LMD.3014": ParcelStatus.IN_TRANSIT,
    "LMD.1004": ParcelStatus.AT_PICKUP_POINT,
    "LMD.1005": ParcelStatus.AT_PICKUP_POINT,
    "LMD.9001": ParcelStatus.AT_PICKUP_POINT,
    "LMD.9002": ParcelStatus.PROBLEM,
    "LMD.9014": ParcelStatus.RETURNING,
    # Terminal outcomes.
    "EOL.1001": ParcelStatus.DELIVERED,
    "EOL.1003": ParcelStatus.DELIVERED,
    "EOL.9001": ParcelStatus.PROBLEM,
    "RTS.1001": ParcelStatus.RETURNING,
    "RTS.1002": ParcelStatus.RETURNING,
}
_unmapped_tracking_statuses_logged: set[str] = set()
_TRACKING_KNOWN_PAYLOAD_KEYS = {
    "trackingNumber", "updatedAt", "status", "statusTitle", "statusDescription",
    "origin", "destination", "trackingDetails",
}
_tracking_payload_shape_logged = False


def _warn_unmapped_tracking_status(code: str) -> None:
    """Report an unmapped public-tracking status once per HA session."""
    if code in _unmapped_tracking_statuses_logged:
        return
    _unmapped_tracking_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised InPost public-tracking status — help us map it. "
        "Open an issue and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def _note_tracking_payload_shape(raw: dict) -> None:
    """One-shot warning for public tracking fields not yet in its schema."""
    global _tracking_payload_shape_logged
    if _tracking_payload_shape_logged:
        return
    extra = sorted(set(raw) - _TRACKING_KNOWN_PAYLOAD_KEYS)
    if extra:
        _tracking_payload_shape_logged = True
        _LOGGER.warning(
            "InPost public-tracking payload carries unconfirmed fields: %s. %s",
            extra,
            NEW_ISSUE_URL,
        )


def tracking_hub_url(tracking_code: str | None, country: str | None) -> str | None:
    """Construct a public-tracking hub's deep-link, per :data:`TRACKING_URL_BY_COUNTRY`.

    Each InPost storefront runs its own tracking page (different host, path
    and query param per country) — unlike the single-country account inbox,
    there is no one template to fall back to.
    """
    if not tracking_code or not country:
        return None
    template = TRACKING_URL_BY_COUNTRY.get(country.upper())
    if not template:
        return None
    return template.format(tracking_code=tracking_code)


def normalize_tracking_parcel(
    raw: dict, *, country: str | None = None, include_history: bool = False
) -> dict:
    """Normalise the keyless ``inposteasy.com`` parcel response.

    This endpoint is structurally independent from the authenticated inbox:
    it has ``trackingDetails`` rather than ``eventLog``, no people, no ETA and
    no locker details.  Do not route it through :func:`normalize_parcel`.
    ``country`` is the tracking hub's own country (its config entry), used
    only to pick the right consumer deep-link — see :func:`tracking_hub_url`.
    """
    _note_tracking_payload_shape(raw)
    code = raw.get("trackingNumber")
    status_code = raw.get("status")
    status = (
        TRACKING_STATUS_MAP.get(status_code.upper())
        if isinstance(status_code, str)
        else None
    )
    if status_code and status is None:
        _warn_unmapped_tracking_status(str(status_code))
    canonical_status = status or ParcelStatus.UNKNOWN
    details = raw.get("trackingDetails")
    history: list[dict] | None = None
    delivered_at = None
    if isinstance(details, list):
        ordered: list[tuple[datetime, dict]] = []
        unparseable: list[dict] = []
        for detail in details:
            if not isinstance(detail, dict):
                continue
            event_code = detail.get("status")
            event_status = (
                TRACKING_STATUS_MAP.get(event_code.upper())
                if isinstance(event_code, str)
                else None
            )
            if event_code and event_status is None:
                _warn_unmapped_tracking_status(str(event_code))
            timestamp = to_iso_timestamp(detail.get("datetime"))
            event = {
                "timestamp": timestamp,
                "status": event_status,
                "raw_status": detail.get("statusTitle") or event_code,
            }
            parsed = parse_iso(timestamp)
            if parsed is None:
                unparseable.append(event)
            else:
                ordered.append((parsed, event))
                if canonical_status is ParcelStatus.DELIVERED and event_status is ParcelStatus.DELIVERED:
                    delivered_at = timestamp
        ordered.sort(key=lambda item: item[0])
        if include_history:
            history = ([event for _, event in ordered] + unparseable)[-HISTORY_MAX_EVENTS:]
    delivered = canonical_status is ParcelStatus.DELIVERED
    return {
        "carrier": "InPost",
        "barcode": code,
        "sender": None,
        "receiver": None,
        "status": canonical_status,
        "raw_status": raw.get("statusTitle") or status_code,
        "delivered": delivered,
        "delivered_at": delivered_at if delivered else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": tracking_hub_url(code, country),
        "weight": None,
        "dimensions": None,
        "history": history,
        "raw": raw,
    }
