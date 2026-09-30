"""Tests for the public-tracking normaliser."""
import pytest

from custom_components.inpost.const import CAPABILITIES_BY_VARIANT, ParcelStatus
from custom_components.inpost.tracking import parcels as parcels_module
from custom_components.inpost.tracking.parcels import (
    normalize_tracking_parcel,
    tracking_hub_url,
)


def test_public_tracking_payload_is_independent_and_unknown(caplog):
    parcels_module._unmapped_tracking_statuses_logged.clear()
    parcel = normalize_tracking_parcel(
        {
            "trackingNumber": "PUBLIC-1",
            "status": "cross_border_moving",
            "statusTitle": "Moving",
            "statusDescription": "On its way",
            "origin": {"countryCode": "IT"},
            "destination": {"countryCode": "PT"},
            "trackingDetails": [
                {
                    "status": "cross_border_moving",
                    "statusTitle": "Moving",
                    "datetime": "2026-08-31T12:00:00Z",
                }
            ],
        },
        include_history=True,
    )
    assert parcel["barcode"] == "PUBLIC-1"
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["sender"] is None
    assert parcel["pickup_point"] is None
    assert parcel["history"][0]["timestamp"] == "2026-08-31T12:00:00Z"
    assert parcel["history"][0]["status"] is None
    assert "cross_border_moving" in caplog.text


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("CRE.1001", ParcelStatus.REGISTERED),
        ("MMD.1004", ParcelStatus.IN_TRANSIT),
        ("LMD.1005", ParcelStatus.AT_PICKUP_POINT),
        ("LMD.9002", ParcelStatus.PROBLEM),
        ("RTS.1002", ParcelStatus.RETURNING),
        ("EOL.1003", ParcelStatus.DELIVERED),
    ],
)
def test_public_tracking_statuses_map_from_live_observations(code, expected):
    parcel = normalize_tracking_parcel(
        {"trackingNumber": "REDACTED", "status": code, "trackingDetails": []}
    )
    assert parcel["status"] is expected



def test_tracking_capabilities_match_what_normalize_tracking_parcel_returns():
    """Every declared Tracking capability must come true somewhere in a sample."""
    capabilities = CAPABILITIES_BY_VARIANT["Tracking"]
    delivered = normalize_tracking_parcel(
        {"trackingNumber": "IT123", "status": "EOL.1001"}, country="IT"
    )
    with_history = normalize_tracking_parcel(
        {
            "trackingNumber": "IT123",
            "status": "EOL.1001",
            "trackingDetails": [
                {"status": "EOL.1001", "datetime": "2026-08-31T12:00:00Z"}
            ],
        },
        country="IT",
        include_history=True,
    )

    assert "pickup_point" not in capabilities
    if "url" in capabilities:
        assert delivered["url"] is not None
    if "history" in capabilities:
        assert with_history["history"] is not None


@pytest.mark.parametrize(
    ("country", "expected_host"),
    [
        ("PL", "inpost.pl"),
        ("IT", "inpost.it"),
        ("PT", "inpost.pt"),
        ("GB", "inpost.co.uk"),
        ("ES", "inpost.es"),
    ],
)
def test_tracking_hub_url_per_country(country, expected_host):
    url = tracking_hub_url("CODE123", country)
    assert url is not None
    assert expected_host in url
    assert "CODE123" in url


def test_tracking_hub_url_lowercase_country_and_missing_inputs():
    assert tracking_hub_url("CODE123", "it") == tracking_hub_url("CODE123", "IT")
    assert tracking_hub_url(None, "IT") is None
    assert tracking_hub_url("CODE123", None) is None
    assert tracking_hub_url("CODE123", "FR") is None
