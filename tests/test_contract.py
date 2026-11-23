"""The parcel contract's checks, run over every source's normalised samples."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.inpost.account.countries.it import (
    normalize_parcel as normalize_it,
)
from custom_components.inpost.account.parcels import (
    is_pickup,
    normalize_parcel,
    planned_for_status,
    planned_window,
)
from custom_components.inpost.const import ParcelStatus
from custom_components.inpost.tracking.parcels import normalize_tracking_parcel

from .account.countries.test_it import event as it_event, parcel as it_parcel
from .payloads import delivered_sample, in_transit_sample, ready_sample

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
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

_WARSAW = ZoneInfo("Europe/Warsaw")


def _hub(*codes):
    return lambda: normalize_tracking_parcel(
        {
            "trackingNumber": "1",
            "status": codes[-1],
            "trackingDetails": [
                {"status": code, "datetime": f"2099-05-2{i + 1}T10:00:00Z"}
                for i, code in enumerate(codes)
            ],
        }
    )


SAMPLES = {
    "ready": lambda: normalize_parcel(ready_sample()),
    "in_transit": lambda: normalize_parcel(in_transit_sample()),
    "collected": lambda: normalize_parcel(delivered_sample()),
    "it_active": lambda: normalize_it(
        it_parcel(it_event("in_transit", "2099-05-21T10:00:00Z"))
    ),
    "it_at_pickup": lambda: normalize_it(
        it_parcel(
            it_event("in_transit", "2099-05-21T10:00:00Z"),
            it_event("ready_to_pickup", "2099-05-22T10:00:00Z", "LMD.1004"),
        )
    ),
    "it_no_point": lambda: normalize_it(
        it_parcel(it_event("in_transit", "2099-05-21T10:00:00Z"), pickUp=None)
    ),
    "hub_active": _hub("CRE.1001", "MMD.1004"),
    "hub_at_pickup": _hub("MMD.1004", "LMD.1005"),
    "hub_collected": _hub("LMD.1005", "EOL.1003"),
    "hub_home": _hub("MMD.1004", "EOL.1003"),
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return SAMPLES[request.param]()


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_is_empty(parcel):
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


@pytest.mark.parametrize("name", ["collected", "hub_collected"])
def test_a_collected_pickup_parcel_stays_pickup(name):
    parcel = SAMPLES[name]()
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True


def test_a_collected_locker_parcel_keeps_its_point():
    assert SAMPLES["collected"]()["pickup_point"] == "KRA010"


def test_a_home_delivery_through_the_hub_is_not_pickup():
    assert SAMPLES["hub_home"]()["pickup"] is False


def test_an_italian_pickup_point_needs_the_pickup_block():
    assert SAMPLES["it_at_pickup"]()["pickup"] is True
    assert SAMPLES["it_no_point"]()["pickup_point"] is None


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T15:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            None,
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            date(2026, 4, 29),
            date(2026, 4, 29),
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            "2026-05-01",
            ("2026-04-29T00:00:00+02:00", "2026-05-01T23:59:59+02:00"),
        ),
        (
            "2026-04-29T15:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            None,
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            datetime(2026, 4, 29, 13, 0),
            datetime(2026, 4, 29, 15, 0),
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (None, None, (None, None)),
        ("garbage", None, (None, None)),
        (12, 13, (None, None)),
    ],
)
def test_planned_window_shapes(start, end, expected):
    assert planned_window(start, end, zone=_WARSAW) == expected


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED],
)
def test_planned_for_status_empties_the_window(status):
    assert planned_for_status(status, "a", "b") == (None, None)


def test_planned_for_status_keeps_the_window_otherwise():
    for status in (
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.PROBLEM,
        ParcelStatus.UNKNOWN,
    ):
        assert planned_for_status(status, "a", "b") == ("a", "b")


def test_is_pickup_sources():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.AT_PICKUP_POINT) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [None, ParcelStatus.AT_PICKUP_POINT])
    assert is_pickup(False, ParcelStatus.IN_TRANSIT, [ParcelStatus.IN_TRANSIT]) is False
    assert is_pickup(None, ParcelStatus.DELIVERED) is False
