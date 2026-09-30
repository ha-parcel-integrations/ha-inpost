"""Tests for the Italian account inbox normaliser."""
import pytest

from custom_components.inpost.account.countries import it
from custom_components.inpost.const import CAPABILITIES_BY_VARIANT, ParcelStatus

CODE = "IT1234567890"


def event(status: str, time: str, code: str = "LMD.1001") -> dict:
    return {"eventCode": code, "eventTime": time, "status": status, "statusGroup": "X"}


def parcel(*events: dict, **extra) -> dict:
    return {
        "parcelId": "p-1",
        "primaryParcelNumber": CODE,
        "events": list(events),
        "sender": {"representativeName": "Shop"},
        "receiver": {"representativeName": "Mario"},
        "pickUp": {"location": {"type": "APM", "point": {"name": "MIL01M"}}},
        **extra,
    }


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("AT_THE_ORIGIN", ParcelStatus.REGISTERED),
        ("IN_TRANSIT", ParcelStatus.IN_TRANSIT),
        ("IN_TRANSIT_LAST_MILE", ParcelStatus.OUT_FOR_DELIVERY),
        ("AWAITING_COLLECTION", ParcelStatus.AT_PICKUP_POINT),
        ("DELIVERED", ParcelStatus.DELIVERED),
        ("NOT_DELIVERED", ParcelStatus.PROBLEM),
        ("EXCEPTION", ParcelStatus.PROBLEM),
        ("RETURN", ParcelStatus.RETURNING),
    ],
)
def test_every_catalogue_status_maps(status, expected):
    assert it.normalize_parcel(parcel(event(status, "2026-09-30T10:00:00Z")))["status"] is expected


def test_current_status_is_the_newest_event_whatever_the_order():
    result = it.normalize_parcel(
        parcel(
            event("AWAITING_COLLECTION", "2026-09-30T12:00:00Z", "LMD.1005"),
            event("IN_TRANSIT", "2026-09-29T08:00:00Z", "MMD.1004"),
        ),
        include_history=True,
    )
    assert result["status"] is ParcelStatus.AT_PICKUP_POINT
    assert result["raw_status"] == "LMD.1005"
    assert result["pickup"] is True
    assert result["pickup_point"] == "MIL01M"
    assert [entry["raw_status"] for entry in result["history"]] == ["MMD.1004", "LMD.1005"]


def test_delivered_parcel_carries_its_delivery_time():
    result = it.normalize_parcel(parcel(event("DELIVERED", "2026-09-30T15:00:00Z", "EOL.1001")))
    assert result["delivered"] is True
    assert result["delivered_at"] == "2026-09-30T15:00:00Z"


def test_canonical_fields_and_url():
    result = it.normalize_parcel(parcel(event("IN_TRANSIT", "2026-09-30T10:00:00Z")))
    assert result["sender"] == "Shop"
    assert result["receiver"] == "Mario"
    assert result["url"] == f"https://inpost.it/trova-il-tuo-pacco?number={CODE}"
    assert result["weight"] is None and result["dimensions"] is None
    assert result["history"] is None


def test_parcel_without_events_is_unknown(caplog):
    result = it.normalize_parcel({"primaryParcelNumber": CODE, "events": []})
    assert result["status"] is ParcelStatus.UNKNOWN
    assert result["raw_status"] is None


def test_pickup_point_as_plain_string_or_absent():
    as_text = parcel(event("IN_TRANSIT", "2026-09-30T10:00:00Z"))
    as_text["pickUp"] = {"location": {"point": "MIL01M"}}
    assert it.normalize_parcel(as_text)["pickup_point"] == "MIL01M"
    as_text["pickUp"] = {"location": {"point": {"other": 1}}}
    assert it.normalize_parcel(as_text)["pickup_point"] is None
    as_text["pickUp"] = None
    assert it.normalize_parcel(as_text)["pickup_point"] is None


def test_unknown_status_warns_once_and_is_unknown(caplog):
    raw = parcel(event("TELEPORTED", "2026-09-30T10:00:00Z"))
    assert it.normalize_parcel(raw)["status"] is ParcelStatus.UNKNOWN
    it.normalize_parcel(raw)
    assert caplog.text.count("TELEPORTED") == 1


def test_a_differently_shaped_parcel_warns_once(caplog):
    it.normalize_parcel({"trackingCode": CODE, "status": "DELIVERED"})
    it.normalize_parcel({"trackingCode": CODE, "status": "DELIVERED"})
    assert caplog.text.count("differs from the shape we modelled") == 1
    assert "trackingCode" in caplog.text


def test_modelled_shape_is_silent(caplog):
    it.normalize_parcel(parcel(event("IN_TRANSIT", "2026-09-30T10:00:00Z")))
    assert "differs from the shape" not in caplog.text


def test_italian_capabilities_come_true():
    capabilities = CAPABILITIES_BY_VARIANT["Account (IT)"]
    result = it.normalize_parcel(
        parcel(event("IN_TRANSIT", "2026-09-30T10:00:00Z")), include_history=True
    )
    for field in capabilities:
        assert result[field] is not None, field
    assert "weight" not in capabilities and "dimensions" not in capabilities
