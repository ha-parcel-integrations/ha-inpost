"""Tests for the public-tracking hub's track/untrack services."""
import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inpost.const import (
    CONF_COUNTRY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from custom_components.inpost.services import (
    SERVICE_TRACK_PARCEL,
    SERVICE_UNTRACK_PARCEL,
    async_setup_services,
    async_unload_services,
)


def _tracking_entry(hass, country="IT", codes=()):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"InPost ({country} tracking)",
        unique_id=country,
        data={CONF_COUNTRY: country},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes]},
    )
    entry.add_to_hass(hass)
    return entry


async def test_track_parcel_adds_a_new_code(hass):
    entry = _tracking_entry(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_TRACK_PARCEL,
        {CONF_TRACKING_CODE: " IT123 ", CONF_COUNTRY: "it"},
        blocking=True,
    )

    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: "IT123"}]


async def test_track_parcel_is_a_no_op_for_a_duplicate_code(hass):
    entry = _tracking_entry(hass, codes=["IT123"])
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_TRACK_PARCEL,
        {CONF_TRACKING_CODE: "IT123", CONF_COUNTRY: "IT"},
        blocking=True,
    )

    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: "IT123"}]


async def test_track_parcel_rejects_a_blank_code(hass):
    _tracking_entry(hass)
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_TRACK_PARCEL,
            {CONF_TRACKING_CODE: "   ", CONF_COUNTRY: "IT"},
            blocking=True,
        )


async def test_track_parcel_rejects_an_unconfigured_country(hass):
    _tracking_entry(hass, country="IT")
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_TRACK_PARCEL,
            {CONF_TRACKING_CODE: "PT999", CONF_COUNTRY: "PT"},
            blocking=True,
        )


async def test_untrack_parcel_removes_a_code(hass):
    entry = _tracking_entry(hass, codes=["IT123", "IT456"])
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_UNTRACK_PARCEL,
        {CONF_TRACKING_CODE: "IT123", CONF_COUNTRY: "IT"},
        blocking=True,
    )

    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: "IT456"}]


async def test_untrack_parcel_is_a_no_op_for_an_unknown_code(hass):
    entry = _tracking_entry(hass, codes=["IT123"])
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_UNTRACK_PARCEL,
        {CONF_TRACKING_CODE: "NOT-THERE", CONF_COUNTRY: "IT"},
        blocking=True,
    )

    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: "IT123"}]


async def test_setup_services_is_idempotent(hass):
    _tracking_entry(hass)
    async_setup_services(hass)
    async_setup_services(hass)

    assert hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL)


async def test_unload_services_keeps_them_while_another_tracking_hub_remains(hass):
    it_entry = _tracking_entry(hass, country="IT")
    _tracking_entry(hass, country="PT")
    async_setup_services(hass)

    async_unload_services(hass, it_entry)

    assert hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL)


async def test_unload_services_removes_them_once_the_last_hub_is_gone(hass):
    """The entry being unloaded is still in async_entries() at this point —
    it must not count itself as a remaining hub."""
    entry = _tracking_entry(hass, country="IT")
    async_setup_services(hass)

    async_unload_services(hass, entry)

    assert not hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL)
    assert not hass.services.has_service(DOMAIN, SERVICE_UNTRACK_PARCEL)


async def test_unload_services_ignores_an_account_entry(hass):
    """An account entry has no CONF_COUNTRY in its data — must not count as a hub."""
    MockConfigEntry(
        domain=DOMAIN,
        title="600123456",
        unique_id="600123456",
        data={"phone": "600123456"},
    ).add_to_hass(hass)
    entry = _tracking_entry(hass, country="IT")
    async_setup_services(hass)

    async_unload_services(hass, entry)

    assert not hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL)
