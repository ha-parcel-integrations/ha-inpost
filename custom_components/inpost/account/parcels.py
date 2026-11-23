"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

The InPost-specific parts are the status mapping and :func:`normalize_parcel`.
Everything else — the timestamp parsing, the history builder, the sort contract,
the delivered filter, the one-shot warning — is suite-wide machinery.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry

from ..const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-inpost/issues/new"
    "?template=unrecognised_status.yml"
)

# Detailed statuses we have already warned about, so each unmapped one is
# logged only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()

# The parcel field names are modelled from InPost's documented mobile API and
# confirmed against one real account (2026-08-15), but the set below is still
# what we've actually seen, not the full API surface.
# Any payload with top-level fields beyond this logs them once — keys only,
# never values (they carry names / a locker open-code) — so we can wire up
# anything we missed. See NEW_ISSUE_URL.
_KNOWN_PAYLOAD_KEYS = {
    "shipmentNumber",
    "status",
    "statusGroup",
    "sender",
    "receiver",
    "pickUpDate",
    "returnedToSenderDate",
    "operations",
    "pickUpPoint",
    "eventLog",
    "expiryDate",
    "openCode",
    "qrCode",
    "parcelSize",
    "shipmentType",
    "storedDate",
    # Confirmed 2026-09-09: locker parcels split across multiple compartments
    # ({uuid, shipmentNumbers[], presentation, collected}). Doesn't feed the
    # canonical shape — a multi-compartment parcel is still one shipment with
    # one status — so it just stays under ``raw``.
    "multiCompartment",
    # Confirmed 2026-08-15 against a real account (see NEW_ISSUE_URL history);
    # none of these feed the canonical shape, they just stay under ``raw``.
    "cod",
    "transactionStatus",
    "avizoTransactionStatus",
    "endOfWeekCollection",
    "economyParcel",
    "internationalParcel",
    "ownershipStatus",
    "sharedTo",
    "refreshUntil",
    "requestEasyAccessZone",
    "voicebot",
    "canShareToObserve",
    "canShareOpenCode",
    "canShareParcel",
    "withDonations",
}
_payload_shape_logged = False

# The consumer tracking deep link, for the parcel's ``url`` field.
TRACKING_URL = "https://inpost.pl/sledzenie-przesylek?number={tracking_code}"

# InPost reports status at two granularities on every parcel:
#   * ``status``      — a detailed, free-form string (~60 values), mapped below.
#   * ``statusGroup`` — a coarse UPPERCASE bucket used as a fallback.
# The two-tier design is deliberate: even a detailed status we have not mapped
# still lands in a sensible canonical bucket via its group, rather than
# ``unknown``. Only a status whose *group* is also unrecognised warns.
#
# Locker vocabulary is the interesting part — InPost has real "ready in the
# locker" and "pickup deadline expired" states no doorstep carrier has. Mapped
# from InPost's documented status list; verify against a live account.

# Detailed ``status`` string -> ParcelStatus. Lower-case keys; the wire values
# are lower-case (e.g. ``ready_to_pickup``).
STATUS_MAP: dict[str, ParcelStatus] = {
    # Announced by the sender, not yet in the network.
    "created": ParcelStatus.REGISTERED,
    "confirmed": ParcelStatus.REGISTERED,
    "dispatched_by_sender": ParcelStatus.REGISTERED,
    "dispatched_by_sender_to_pok": ParcelStatus.REGISTERED,
    # Moving through the network. Live-confirmed 2026-09-09 (real account,
    # multi-parcel event logs): ``taken_by_courier`` fires right after
    # ``confirmed``/``dispatched_by_sender`` and well before
    # ``adopted_at_source_branch`` — the courier has physically collected the
    # parcel from the sender, it is not yet anywhere near the recipient. It
    # was previously bucketed with the terminal "delivered" states, which made
    # a mid-journey parcel's history jump to ``delivered`` and back to
    # ``in_transit``. ``collected_from_sender`` shares the same "sender-side
    # collection" semantics by name and is corrected alongside it.
    "taken_by_courier": ParcelStatus.IN_TRANSIT,
    "taken_by_courier_from_pok": ParcelStatus.IN_TRANSIT,
    "collected_from_sender": ParcelStatus.IN_TRANSIT,
    "adopted_at_source_branch": ParcelStatus.IN_TRANSIT,
    "sent_from_source_branch": ParcelStatus.IN_TRANSIT,
    "adopted_at_sorting_center": ParcelStatus.IN_TRANSIT,
    "sent_from_sorting_center": ParcelStatus.IN_TRANSIT,
    "adopted_at_target_branch": ParcelStatus.IN_TRANSIT,
    "redirect_to_box": ParcelStatus.IN_TRANSIT,
    "permanently_redirected_to_box_machine": ParcelStatus.IN_TRANSIT,
    "permanently_redirected_to_customer_service_point": ParcelStatus.IN_TRANSIT,
    "readdressed": ParcelStatus.IN_TRANSIT,
    # Final leg to the door or the locker.
    "out_for_delivery": ParcelStatus.OUT_FOR_DELIVERY,
    "out_for_delivery_to_address": ParcelStatus.OUT_FOR_DELIVERY,
    # Waiting for the recipient in a locker or point.
    "ready_to_pickup": ParcelStatus.AT_PICKUP_POINT,
    "ready_for_collection": ParcelStatus.AT_PICKUP_POINT,
    "ready_to_pickup_from_branch": ParcelStatus.AT_PICKUP_POINT,
    "ready_to_pickup_from_pok": ParcelStatus.AT_PICKUP_POINT,
    "ready_to_pickup_from_pok_registered": ParcelStatus.AT_PICKUP_POINT,
    "stack_in_box_machine": ParcelStatus.AT_PICKUP_POINT,
    "stack_in_customer_service_point": ParcelStatus.AT_PICKUP_POINT,
    "pickup_reminder_sent": ParcelStatus.AT_PICKUP_POINT,
    # Despite the name, still collectible: a parcel left in a temporary locker
    # after its storage window keeps statusGroup TO_PICKUP, a valid openCode
    # and operations.collect, and the app offers it for pickup (seen live).
    "stack_parcel_in_box_machine_pickup_time_expired": ParcelStatus.AT_PICKUP_POINT,
    # Collected / delivered — terminal "arrived" states, recipient-side only.
    # ``claimed`` is the post-pickup state of a locker parcel, so it sorts
    # with delivered, never mid-transit.
    "delivered": ParcelStatus.DELIVERED,
    "collected_by_customer": ParcelStatus.DELIVERED,
    "claimed": ParcelStatus.DELIVERED,
    # Going back to the sender.
    "returned_to_sender": ParcelStatus.RETURNING,
    "returned_to_source_branch": ParcelStatus.RETURNING,
    "return_pickup_confirmation_to_sender": ParcelStatus.RETURNING,
    # Something went wrong.
    "delay_in_delivery": ParcelStatus.PROBLEM,
    "delivery_attempt_failed": ParcelStatus.PROBLEM,
    # Not a locker reminder: the courier found nobody at the address (event
    # LMD.9004, group IN_DELIVERY) and the parcel is not in a locker. Read
    # from a third-party integration's regression fix.
    "pickup_reminder_sent_address": ParcelStatus.PROBLEM,
    "rejected_by_receiver": ParcelStatus.PROBLEM,
    "not_collected": ParcelStatus.PROBLEM,
    "missing": ParcelStatus.PROBLEM,
    "oversized": ParcelStatus.PROBLEM,
    "canceled": ParcelStatus.PROBLEM,
    "cancelled": ParcelStatus.PROBLEM,
    "pickup_time_expired": ParcelStatus.PROBLEM,
    "stack_parcel_pickup_time_expired": ParcelStatus.PROBLEM,
    # Live-confirmed 2026-08-15 (real account): a bare "avizo" preceded by
    # ``rejected_by_receiver`` in the parcel's own event log — a delivery
    # attempt the receiver turned away, not a locker-ready state. Distinct
    # from ``avizo_rejected`` above.
    "avizo": ParcelStatus.PROBLEM,
    "avizo_rejected": ParcelStatus.PROBLEM,
    "undelivered": ParcelStatus.PROBLEM,
    "undelivered_cod_cash_receiver": ParcelStatus.PROBLEM,
    "undelivered_incomplete_address": ParcelStatus.PROBLEM,
    "undelivered_lack_of_access_letterbox": ParcelStatus.PROBLEM,
    "undelivered_no_mailbox": ParcelStatus.PROBLEM,
    "undelivered_not_live_address": ParcelStatus.PROBLEM,
    "undelivered_unknown_receiver": ParcelStatus.PROBLEM,
    "undelivered_wrong_address": ParcelStatus.PROBLEM,
}

# Coarse ``statusGroup`` -> ParcelStatus. Matched case-insensitively (the wire
# values are UPPERCASE). Confirmed values only: ``in_delivery`` from a real
# account payload (2026-08-15), ``to_send``/``to_pickup``/``delivered`` from
# another (2026-09-09). The original guess also carried ``created``/``ready``/
# ``claimed``/``other`` from public docs — none of those four have ever been
# seen on the wire, so they were dropped rather than left as dead weight;
# re-add one only once a real payload shows it.
STATUS_GROUP_MAP: dict[str, ParcelStatus] = {
    "to_send": ParcelStatus.REGISTERED,
    "in_delivery": ParcelStatus.IN_TRANSIT,
    "to_pickup": ParcelStatus.AT_PICKUP_POINT,
    "delivered": ParcelStatus.DELIVERED,
}

# Payment events, not steps in the parcel's journey: a cash-on-delivery parcel
# can be paid for while it is still moving or already waiting in the locker.
# They say nothing about where the parcel is, so the parcel keeps the bucket
# its ``statusGroup`` gives it and a history entry keeps ``status: None``,
# without the unmapped-status warning.
PAYMENT_STATUSES = frozenset({"cod_completed", "c2x_completed"})


def _note_payload_shape(raw: dict) -> None:
    """One-shot: report unconfirmed top-level fields so a tester can map them."""
    global _payload_shape_logged  # noqa: PLW0603 -- one-shot warning flag, module-level by design
    if _payload_shape_logged:
        return
    extra = sorted(set(raw) - _KNOWN_PAYLOAD_KEYS)
    if not extra:
        return
    _payload_shape_logged = True
    _LOGGER.warning(
        "InPost payload carries fields we have not confirmed against a real "
        "account yet: %s. Please help us map them — a diagnostics file is "
        "ideal: %s",
        extra,
        NEW_ISSUE_URL,
    )


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped detailed status once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised InPost status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_parcel_status(status: str | None, status_group: str | None) -> ParcelStatus:
    """Map InPost's two-tier status onto a canonical :class:`ParcelStatus`.

    The detailed ``status`` string is tried first (:data:`STATUS_MAP`). When it
    is unmapped, the coarse ``statusGroup`` (:data:`STATUS_GROUP_MAP`) still
    lands the parcel in a sensible bucket rather than ``unknown`` — but an
    unmapped *detailed* status is still reported, because that is how the
    detailed map gets completed. Both empty → ``unknown``, silently (a parcel
    with no status yet is a normal, transient state).
    """
    key = status.strip().lower() if status else None
    detailed = STATUS_MAP.get(key) if key else None
    if detailed is not None:
        return detailed

    grouped = (
        STATUS_GROUP_MAP.get(status_group.strip().lower()) if status_group else None
    )
    if status and key not in PAYMENT_STATUSES:
        # Report the unmapped detailed value even when the group saved the day.
        _warn_unmapped_status(status)
    return grouped if grouped is not None else ParcelStatus.UNKNOWN


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Numbers are treated as **epoch milliseconds** — the common case for the
    consumer APIs in this suite. Strings pass through untouched; their
    consumers are guarded by :func:`parse_iso`. Adjust the numeric branch if
    your carrier stamps in seconds.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def build_history(
    event_log: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from InPost's ``eventLog[]``.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. Live-confirmed 2026-08-15: each entry's
    ``name`` shares the exact same vocabulary as the parcel's own ``status``
    field (e.g. an ``eventLog`` entry named ``AVIZO`` lines up with a parcel
    whose ``status`` is ``avizo``), so it is mapped through :data:`STATUS_MAP`
    like the top-level status rather than kept as free text. An unmapped name
    still reports — deduped against the same set the main status warning uses,
    since it is the same vocabulary — but keeps ``status: None``. Sorted
    oldest → newest and capped to the most recent ``max_events``.
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for event in event_log or []:
        if not isinstance(event, dict):
            continue
        timestamp = to_iso_timestamp(event.get("date"))
        if not timestamp:
            continue
        name = event.get("name")
        key = name.strip().lower() if name else None
        status = STATUS_MAP.get(key) if key else None
        if name and status is None and key not in PAYMENT_STATUSES:
            _warn_unmapped_status(name)
        entry = {
            "timestamp": timestamp,
            "status": status,
            "raw_status": name,
        }
        parsed = parse_iso(timestamp)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the account inbox's consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def _name_of(customer: Any) -> str | None:
    """Return the ``.name`` of an InPost customer object, or ``None``."""
    # A live account showed ``sender`` as a bare string, not an object.
    name = customer.get("name") if isinstance(customer, dict) else customer
    if isinstance(name, str) and name.strip():
        return name
    return None


def _pickup_point_name(point: Any) -> str | None:
    """Return a human label for the parcel's Paczkomat / pickup point."""
    if not isinstance(point, dict):
        return None
    for key in ("name", "locationDescription"):
        value = point.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED}
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract** — every carrier returns
    exactly these, in this order, and the aggregator depends on it. A key InPost
    does not expose is ``None``, never omitted.

    InPost specifics worth knowing:

    * **Status is two-tier** — detailed ``status`` plus a coarse ``statusGroup``
      fallback; see :func:`map_parcel_status`.
    * **No delivery window** is exposed, so ``planned_from`` / ``planned_to``
      are always ``None``. ``expiryDate`` is a *pickup deadline*, not a delivery
      ETA, and stays under ``raw`` — a locker parcel's "collect me by" date.
    * ``delivered_at`` is the collection time (``pickUpDate``), or the return
      time for a returned parcel — the timestamps reliably present once a parcel
      is finished.
    * **The locker is the pickup point.** ``pickUpPoint`` carries the Paczkomat
      name; the ``openCode`` / ``qrCode`` needed to physically open it stay
      under ``raw`` (a QR-display entity is a possible fast-follow, not in this
      release).
    * ``weight`` / ``dimensions`` are ``None`` — InPost exposes only a size
      *class* (``parcelSize``, a letter), which stays under ``raw``.
    """
    _note_payload_shape(raw)
    tracking_code = raw.get("shipmentNumber")
    status = map_parcel_status(raw.get("status"), raw.get("statusGroup"))
    delivered = status is ParcelStatus.DELIVERED

    delivered_at = None
    if delivered:
        delivered_at = to_iso_timestamp(
            raw.get("pickUpDate") or raw.get("returnedToSenderDate")
        )

    operations = raw.get("operations") or {}
    point_name = _pickup_point_name(raw.get("pickUpPoint"))
    pickup = is_pickup(
        bool(operations.get("collect") or point_name),
        status,
        (
            STATUS_MAP.get(str(event.get("name")).strip().lower())
            for event in raw.get("eventLog") or []
            if isinstance(event, dict) and event.get("name")
        ),
    )

    return {
        "carrier": "InPost",
        "barcode": tracking_code,
        "sender": _name_of(raw.get("sender")),
        "receiver": _name_of(raw.get("receiver")),
        "contents": None,
        "status": status,
        "raw_status": raw.get("status"),
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": None,
        "planned_to": None,
        "pickup": pickup,
        "pickup_point": point_name if pickup else None,
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": build_history(raw.get("eventLog")) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
