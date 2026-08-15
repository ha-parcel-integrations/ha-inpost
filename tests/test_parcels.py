"""Tests for the pure parcel-mapping helpers.

No Home Assistant instance needed — the point of keeping ``parcels.py`` free of
I/O is that the InPost-specific mapping can be tested as plain functions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inpost import parcels as parcels_module
from custom_components.inpost.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.inpost.parcels import (
    apply_delivered_filter,
    build_history,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
)

from .payloads import (
    ACTIVE_CODE,
    delivered_sample,
    in_transit_sample,
    ready_sample,
)

# ---------------------------------------------------------------------------
# two-tier status mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        ("created", ParcelStatus.REGISTERED),
        ("adopted_at_sorting_center", ParcelStatus.IN_TRANSIT),
        ("out_for_delivery", ParcelStatus.OUT_FOR_DELIVERY),
        ("ready_to_pickup", ParcelStatus.AT_PICKUP_POINT),
        ("stack_in_box_machine", ParcelStatus.AT_PICKUP_POINT),
        ("collected_by_customer", ParcelStatus.DELIVERED),
        ("claimed", ParcelStatus.DELIVERED),
        ("returned_to_sender", ParcelStatus.RETURNING),
        ("delivery_attempt_failed", ParcelStatus.PROBLEM),
        ("undelivered_wrong_address", ParcelStatus.PROBLEM),
        ("avizo", ParcelStatus.PROBLEM),
    ],
)
def test_detailed_status_maps(status, expected):
    assert map_parcel_status(status, None) == expected


def test_status_is_case_insensitive():
    assert map_parcel_status("READY_TO_PICKUP", None) == ParcelStatus.AT_PICKUP_POINT
    assert map_parcel_status(" ready_to_pickup ", None) == ParcelStatus.AT_PICKUP_POINT


def test_group_is_the_fallback_when_detailed_status_unmapped(caplog):
    """An unmapped detailed status still lands in a sensible bucket via its
    group — but is still reported so the detailed map can be completed."""
    result = map_parcel_status("some_brand_new_status", "IN_DELIVERY")
    assert result == ParcelStatus.IN_TRANSIT
    assert "some_brand_new_status" in caplog.text


def test_group_claimed_counts_as_delivered():
    assert map_parcel_status("weird", "CLAIMED") == ParcelStatus.DELIVERED


def test_unmapped_status_and_group_is_unknown(caplog):
    assert map_parcel_status("teleported", "OTHER") == ParcelStatus.UNKNOWN
    assert "teleported" in caplog.text
    assert "issues/new" in caplog.text


def test_missing_status_is_unknown_and_silent(caplog):
    assert map_parcel_status(None, None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("", "") == ParcelStatus.UNKNOWN
    assert caplog.text == ""


def test_unmapped_status_warns_only_once(caplog):
    map_parcel_status("abducted", None)
    map_parcel_status("abducted", None)
    assert caplog.text.count("abducted") == 1


def test_payload_shape_warns_once_for_unconfirmed_fields(caplog):
    """A payload with fields beyond the modelled set logs them once, keys only,
    with an issue link — so a tester can confirm the shape."""
    parcels_module._payload_shape_logged = False
    raw = {
        "shipmentNumber": "6200000000000000000000",
        "status": "delivered",
        "customerReference": "secret-order-note",
    }
    normalize_parcel(raw)
    normalize_parcel(raw)
    assert caplog.text.count("have not confirmed against a real") == 1
    assert "customerReference" in caplog.text
    assert "secret-order-note" not in caplog.text  # keys only, never values
    assert "issues/new" in caplog.text


def test_payload_shape_silent_for_known_fields(caplog):
    """The modelled field set logs nothing."""
    parcels_module._payload_shape_logged = False
    normalize_parcel({"shipmentNumber": "6200000000000000000000", "status": "delivered"})
    assert "have not confirmed against a real" not in caplog.text


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def test_build_history_orders_oldest_to_newest():
    history = build_history(delivered_sample()["eventLog"])
    assert history[0]["raw_status"] == "ADOPTED_AT_SORTING_CENTER"
    assert history[-1]["raw_status"] == "DELIVERED"
    # eventLog names share the status vocabulary, so they map cleanly.
    assert history[0]["status"] == ParcelStatus.IN_TRANSIT
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_handles_missing_and_malformed():
    assert build_history(None) == []
    assert build_history([{"name": "X"}]) == []  # no date
    assert build_history(["not-a-dict"]) == []


def test_build_history_maps_status_from_event_name():
    events = [{"name": "DELIVERED", "date": "2026-04-30T18:22:00+02:00"}]
    entry = build_history(events)[0]
    assert entry["raw_status"] == "DELIVERED"
    assert entry["status"] == ParcelStatus.DELIVERED


def test_build_history_warns_on_unmapped_event_name(caplog):
    events = [{"name": "SOME_NEW_EVENT_TYPE", "date": "2026-04-30T18:22:00+02:00"}]
    entry = build_history(events)[0]
    assert entry["status"] is None
    assert "SOME_NEW_EVENT_TYPE" in caplog.text


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def test_normalize_publishes_exactly_the_canonical_keys():
    assert list(normalize_parcel(ready_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport InPost on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES


def test_capabilities_omit_weight_dimensions_and_delivery_window():
    """InPost never exposes these — CAPABILITIES must not claim otherwise."""
    assert "weight" not in CAPABILITIES
    assert "dimensions" not in CAPABILITIES
    assert "delivery_window" not in CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Every declared CAPABILITIES entry must come true somewhere in a sample."""
    delivered = normalize_parcel(delivered_sample())
    pickup = normalize_parcel(ready_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    if "pickup_point" in CAPABILITIES:
        assert pickup["pickup_point"] is not None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "history" in CAPABILITIES:
        assert with_history["history"] is not None


def test_normalize_ready_parcel_is_a_pickup():
    parcel = normalize_parcel(ready_sample())
    assert parcel["carrier"] == "InPost"
    assert parcel["barcode"] == ACTIVE_CODE
    assert parcel["sender"] == "Allegro"
    assert parcel["receiver"] == "Jan Kowalski"
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["raw_status"] == "ready_to_pickup"
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "KRA010"
    assert parcel["delivered"] is False
    assert parcel["url"].endswith(ACTIVE_CODE)
    assert parcel["history"] is None  # opt-in, default off


def test_normalize_collect_flag_alone_marks_pickup():
    """Even a status we would not bucket as pickup counts as collectable when
    InPost sets ``operations.collect``."""
    raw = ready_sample()
    raw["status"] = "some_unmapped_ready_variant"
    raw["statusGroup"] = "READY"
    parcel = normalize_parcel(raw)
    assert parcel["pickup"] is True


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-30T18:22:00+02:00"
    assert parcel["pickup"] is False


def test_normalize_leaves_unexposed_fields_none():
    parcel = normalize_parcel(in_transit_sample())
    for key in ("planned_from", "planned_to", "weight", "dimensions", "pickup_point"):
        assert parcel[key] is None, key
    assert parcel["status"] == ParcelStatus.IN_TRANSIT


def test_normalize_history_is_opt_in():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 4


def test_normalize_keeps_raw_payload():
    raw = ready_sample()
    assert normalize_parcel(raw)["raw"] is raw


def test_normalize_blank_names_become_none():
    raw = in_transit_sample()
    raw["sender"] = {"name": ""}
    raw["receiver"] = None
    parcel = normalize_parcel(raw)
    assert parcel["sender"] is None
    assert parcel["receiver"] is None


# ---------------------------------------------------------------------------
# sorting and the delivered filter
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id="600123456",
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels


def test_parse_iso_handles_zone_naive_and_garbage():
    assert parse_iso("2026-04-30T18:22:00+02:00").tzinfo is not None
    assert parse_iso("2026-04-30T18:22:00").tzinfo == timezone.utc
    assert parse_iso("nope") is None
    assert parse_iso(None) is None


def test_history_unparseable_timestamp_sorts_last():
    events = [
        {"name": "CONFIRMED", "date": "2026-04-28T09:00:00+02:00"},
        {"name": "SOME_UNKNOWN_EVENT", "date": "not-a-date"},
    ]
    history = build_history(events)
    assert [e["raw_status"] for e in history] == ["CONFIRMED", "SOME_UNKNOWN_EVENT"]


def test_url_is_none_without_a_barcode():
    parcel = normalize_parcel({"status": "created"})
    assert parcel["barcode"] is None
    assert parcel["url"] is None


def test_pickup_point_falls_back_to_location_description():
    raw = ready_sample()
    raw["pickUpPoint"] = {"locationDescription": "Obok Żabki"}
    assert normalize_parcel(raw)["pickup_point"] == "Obok Żabki"


def test_pickup_point_non_dict_is_none():
    raw = ready_sample()
    raw["pickUpPoint"] = "KRA010"  # unexpected shape
    assert normalize_parcel(raw)["pickup_point"] is None
