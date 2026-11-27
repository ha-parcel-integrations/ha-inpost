"""Tests for the contents action, the track_parcel field and the store lifecycle."""

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.inpost.account.coordinator import InPostCoordinator
from custom_components.inpost.const import (
    CONF_AUTH_TOKEN,
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_PHONE,
    CONF_REFRESH_TOKEN,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from custom_components.inpost.services import async_setup_services
from custom_components.inpost.tracking import coordinator as module

from .payloads import (
    ACTIVE_CODE as ACCOUNT_CODE,
    DELIVERED_CODE,
    delivered_sample,
    ready_sample,
)

HUB_CLIENT = "custom_components.inpost.tracking.client.InPostTrackingApiClient"
ACCOUNT_GET = (
    "custom_components.inpost.account.client.InPostApiClient.async_get_parcels"
)
HUB_CODE = "IT123"
UNTRACKED = "IT999"
STORE_KEY = f"{DOMAIN}.contents.%s"


def _hub_payload(code, status="MMD.1001"):
    return {"trackingNumber": code, "status": status}


async def _setup(hass, codes=None, country="IT"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=country,
        data={CONF_COUNTRY: country},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes or []]},
    )
    entry.add_to_hass(hass)
    with patch(
        f"{HUB_CLIENT}.async_get_parcel",
        new=AsyncMock(side_effect=_hub_payload),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _setup_account(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="600123456",
        data={
            CONF_PHONE: "600123456",
            CONF_AUTH_TOKEN: "acc",
            CONF_REFRESH_TOKEN: "ref",
        },
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 5,
        },
    )
    entry.add_to_hass(hass)
    with patch(ACCOUNT_GET, new=AsyncMock(return_value=_account_raws())):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _account_raws():
    return [ready_sample(), delivered_sample()]


@contextmanager
def _polls():
    with (
        patch(
            f"{HUB_CLIENT}.async_get_parcel", new=AsyncMock(side_effect=_hub_payload)
        ),
        patch(ACCOUNT_GET, new=AsyncMock(return_value=_account_raws())),
    ):
        yield


async def _call(hass, service: str, data: dict) -> None:
    with _polls():
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()
        # A refresh requested inside the debouncer's cooldown runs after it.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()


@contextmanager
def _no_poll():
    """Fail the test's expectation if a service asks either backend for data."""
    with (
        patch.object(
            InPostCoordinator, "async_request_refresh", AsyncMock()
        ) as refresh,
        patch(f"{HUB_CLIENT}.async_get_parcel", new=AsyncMock()) as fetch,
        patch(ACCOUNT_GET, new=fetch),
    ):
        yield refresh, fetch


@contextmanager
def _listener_count(*entries):
    """Count the listener notifications of the given entries' coordinators."""
    coordinators = [entry.runtime_data.coordinator for entry in entries]
    with patch.object(
        InPostCoordinator, "async_update_listeners", autospec=True
    ) as notify:
        yield lambda: sum(
            1 for call in notify.call_args_list if call.args[0] in coordinators
        )


async def _quiet_call(hass, service: str, data: dict) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True)
    await hass.async_block_till_done()


def _contents(entry, code):
    parcels = {p["barcode"]: p for p in entry.runtime_data.coordinator.data}
    return parcels[code]["contents"]


def _store(entry):
    return entry.runtime_data.coordinator.contents_store


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = await _setup(hass, [HUB_CODE])
    assert _contents(entry, HUB_CODE) is None


async def test_update_contents_sets_replaces_and_clears(hass):
    entry = await _setup(hass, [HUB_CODE])

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": " Running  shoes "},
    )
    assert _contents(entry, HUB_CODE) == "Running shoes"

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Winter socks"},
    )
    assert _contents(entry, HUB_CODE) == "Winter socks"

    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: HUB_CODE, "contents": "  "}
    )
    assert _contents(entry, HUB_CODE) is None


async def test_update_contents_without_text_clears(hass):
    entry = await _setup(hass, [HUB_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )

    await _call(hass, "update_contents", {CONF_TRACKING_CODE: HUB_CODE})

    assert _contents(entry, HUB_CODE) is None


async def test_update_contents_normalizes_the_code(hass):
    entry = await _setup(hass, ["IT123"])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: " it123 ", "contents": "Running shoes"},
    )
    assert _store(entry).get("IT123") == "Running shoes"


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    entry = await _setup(hass, [HUB_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )

    assert _store(entry).get(UNTRACKED) == "Running shoes"
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: HUB_CODE}]


async def test_update_contents_reapplies_only_when_something_changed(hass):
    entry = await _setup(hass, [HUB_CODE])
    data = {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"}
    with _no_poll() as (refresh, fetch), _listener_count(entry) as count:
        await _quiet_call(hass, "update_contents", data)
        assert count() == 1
        await _quiet_call(hass, "update_contents", data)
        assert count() == 1
        await _quiet_call(hass, "update_contents", {CONF_TRACKING_CODE: UNTRACKED})
        assert count() == 1
    assert _contents(entry, HUB_CODE) == "Running shoes"
    refresh.assert_not_called()
    fetch.assert_not_called()


async def test_an_unchanged_value_writes_nothing(hass):
    entry = await _setup(hass, [HUB_CODE])
    store = _store(entry)
    with patch.object(store._store, "async_delay_save") as save:
        assert store.async_set(HUB_CODE, "Running shoes") is True
        assert save.call_count == 1
        assert store.async_set(HUB_CODE, " Running shoes ") is False
        assert store.async_set(UNTRACKED, None) is False
        assert save.call_count == 1


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: " "})


async def test_update_contents_needs_a_loaded_entry(hass):
    async_setup_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: HUB_CODE}, blocking=True
        )


async def test_update_contents_ignores_an_entry_that_is_not_loaded(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
        )


async def test_track_parcel_stores_contents_for_a_new_code(hass):
    entry = await _setup(hass)
    await _call(
        hass,
        "track_parcel",
        {CONF_TRACKING_CODE: HUB_CODE, CONF_COUNTRY: "IT", "contents": "Running shoes"},
    )

    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: HUB_CODE}]
    assert _contents(entry, HUB_CODE) == "Running shoes"


async def test_track_parcel_without_contents_is_unchanged(hass):
    entry = await _setup(hass)
    await _call(
        hass, "track_parcel", {CONF_TRACKING_CODE: HUB_CODE, CONF_COUNTRY: "IT"}
    )

    assert _store(entry).get(HUB_CODE) is None
    assert _contents(entry, HUB_CODE) is None


async def test_track_parcel_replaces_and_clears_contents_of_a_tracked_code(hass):
    entry = await _setup(hass, [HUB_CODE])
    track = {CONF_TRACKING_CODE: HUB_CODE, CONF_COUNTRY: "IT"}

    await _call(hass, "track_parcel", {**track, "contents": "Running shoes"})
    assert _contents(entry, HUB_CODE) == "Running shoes"

    await _call(hass, "track_parcel", {**track, "contents": "Winter socks"})
    assert _contents(entry, HUB_CODE) == "Winter socks"

    await _call(hass, "track_parcel", {**track, "contents": ""})
    assert _contents(entry, HUB_CODE) is None
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: HUB_CODE}]


async def test_track_parcel_on_a_tracked_code_reapplies_without_a_poll(hass):
    entry = await _setup(hass, [HUB_CODE])
    data = {
        CONF_TRACKING_CODE: HUB_CODE,
        CONF_COUNTRY: "IT",
        "contents": "Running shoes",
    }
    with _no_poll() as (refresh, fetch), _listener_count(entry) as count:
        await _quiet_call(hass, "track_parcel", data)
        await _quiet_call(hass, "track_parcel", data)
        assert count() == 1
    assert _contents(entry, HUB_CODE) == "Running shoes"
    refresh.assert_not_called()
    fetch.assert_not_called()


async def test_track_parcel_with_contents_needs_a_loaded_hub(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "track_parcel",
            {
                CONF_TRACKING_CODE: HUB_CODE,
                CONF_COUNTRY: "IT",
                "contents": "Running shoes",
            },
        )


async def test_untrack_parcel_drops_the_contents(hass):
    entry = await _setup(hass, [HUB_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )

    await _call(
        hass, "untrack_parcel", {CONF_TRACKING_CODE: HUB_CODE, CONF_COUNTRY: "IT"}
    )

    assert _store(entry).get(HUB_CODE) is None
    assert entry.options[CONF_PARCELS] == []


async def test_untrack_parcel_skips_a_hub_that_is_not_loaded(hass):
    asleep = await _setup(hass, [HUB_CODE])
    other = await _setup(hass, [], country="PT")
    assert await hass.config_entries.async_unload(asleep.entry_id)
    assert other.state is ConfigEntryState.LOADED

    await hass.services.async_call(
        DOMAIN,
        "untrack_parcel",
        {CONF_TRACKING_CODE: HUB_CODE, CONF_COUNTRY: "IT"},
        blocking=True,
    )

    assert asleep.options[CONF_PARCELS] == []


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    entry = await _setup(hass, [HUB_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )
    await _store(entry)._store.async_save(_store(entry)._data_to_save())

    assert await hass.config_entries.async_unload(entry.entry_id)
    with _polls():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert _contents(entry, HUB_CODE) == "Running shoes"


async def test_an_entry_without_a_store_file_sets_up_and_loads_empty(
    hass, hass_storage
):
    assert not [key for key in hass_storage if key.startswith(f"{DOMAIN}.contents.")]
    entry = await _setup(hass, [HUB_CODE])
    assert entry.state is ConfigEntryState.LOADED
    assert _contents(entry, HUB_CODE) is None


@pytest.mark.parametrize("kind", ["hub", "account"])
async def test_removing_the_entry_removes_its_store(hass, hass_storage, kind):
    if kind == "hub":
        entry, code = await _setup(hass, [HUB_CODE]), HUB_CODE
    else:
        entry, code = await _setup_account(hass), ACCOUNT_CODE
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: code, "contents": "Running shoes"},
    )
    store = _store(entry)
    await store._store.async_save(store._data_to_save())
    key = STORE_KEY % entry.entry_id
    assert key in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_a_contents_change_alone_fires_no_event(hass):
    await _setup(hass, [HUB_CODE])
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", events.append)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )

    assert events == []


# ---------------------------------------------------------------------------
# overlay on the coordinators
# ---------------------------------------------------------------------------


async def test_user_contents_reaches_the_delivered_list_and_events(hass):
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    coordinator.contents_store.async_set(HUB_CODE, "Running shoes")
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 10,
        },
    )
    delivered = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", delivered.append)

    with patch(
        f"{HUB_CLIENT}.async_get_parcel",
        new=AsyncMock(return_value=_hub_payload(HUB_CODE, "EOL.1001")),
    ):
        await coordinator._async_update_data()
        await hass.async_block_till_done()

    assert coordinator.delivered[0]["contents"] == "Running shoes"
    assert delivered[0].data["contents"] == "Running shoes"


async def test_user_contents_is_keyed_on_the_requested_code(hass):
    """A payload carrying another number still finds the value stored for the code."""
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    coordinator.contents_store.async_set(HUB_CODE, "Running shoes")

    with patch(
        f"{HUB_CLIENT}.async_get_parcel",
        new=AsyncMock(return_value=_hub_payload(DELIVERED_CODE)),
    ):
        data = await coordinator._async_update_data()

    assert data[0]["barcode"] == DELIVERED_CODE
    assert data[0]["contents"] == "Running shoes"


async def test_user_contents_overlays_the_carrier_value(hass):
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    real = module.normalize_tracking_parcel

    def carrier_text(raw, **kwargs):
        parcel = real(raw, **kwargs)
        parcel["contents"] = "Carrier text"
        return parcel

    with (
        patch.object(module, "normalize_tracking_parcel", side_effect=carrier_text),
        patch(
            f"{HUB_CLIENT}.async_get_parcel",
            new=AsyncMock(return_value=_hub_payload(HUB_CODE)),
        ),
    ):
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Carrier text"
        )
        coordinator.contents_store.async_set(HUB_CODE, "Running shoes")
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Running shoes"
        )
        coordinator.contents_store.async_set(HUB_CODE, None)
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Carrier text"
        )


async def test_account_overlay_covers_active_and_delivered(hass):
    account = await _setup_account(hass)
    coordinator = account.runtime_data.coordinator
    assert isinstance(coordinator, InPostCoordinator)
    coordinator.contents_store.async_set(ACCOUNT_CODE, "Running shoes")
    coordinator.contents_store.async_set(DELIVERED_CODE, "Winter socks")

    with patch(ACCOUNT_GET, new=AsyncMock(return_value=_account_raws())):
        active = await coordinator._async_update_data()

    assert active[0]["contents"] == "Running shoes"
    assert coordinator.delivered[0]["contents"] == "Winter socks"


async def test_known_codes_list_every_code_the_entry_holds(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)

    assert hub.runtime_data.coordinator.known_codes == {HUB_CODE}
    assert account.runtime_data.coordinator.known_codes == {
        ACCOUNT_CODE,
        DELIVERED_CODE,
    }


async def test_account_entries_take_contents_too(hass):
    account = await _setup_account(hass)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACCOUNT_CODE, "contents": "Running shoes"},
    )

    assert _contents(account, ACCOUNT_CODE) == "Running shoes"


# ---------------------------------------------------------------------------
# several entries
# ---------------------------------------------------------------------------


async def test_update_contents_goes_to_the_entry_that_lists_the_code(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )

    assert _store(hub).get(HUB_CODE) == "Running shoes"
    assert _store(account).get(HUB_CODE) is None


async def test_update_contents_goes_to_every_entry_that_lists_the_code(hass):
    home = await _setup(hass, [HUB_CODE])
    work = await _setup(hass, [HUB_CODE], country="PT")

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )

    assert _store(home).get(HUB_CODE) == "Running shoes"
    assert _store(work).get(HUB_CODE) == "Running shoes"


async def test_update_contents_with_several_entries_and_no_match_needs_an_entry(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)

    with pytest.raises(ServiceValidationError) as error:
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
        )

    assert error.value.translation_key == "update_contents_entry_required"
    assert _store(hub).get(UNTRACKED) is None
    assert _store(account).get(UNTRACKED) is None


async def test_config_entry_id_restricts_and_stores_an_unknown_code(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)

    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: UNTRACKED,
            "contents": "Running shoes",
            "config_entry_id": account.entry_id,
        },
    )
    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: HUB_CODE,
            "contents": "Winter socks",
            "config_entry_id": hub.entry_id,
        },
    )

    assert _store(account).get(UNTRACKED) == "Running shoes"
    assert _store(hub).get(UNTRACKED) is None
    assert _store(hub).get(HUB_CODE) == "Winter socks"
    assert _store(account).get(HUB_CODE) is None


async def test_config_entry_id_must_name_a_loaded_entry(hass):
    await _setup(hass, [HUB_CODE])
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: HUB_CODE, "config_entry_id": "missing"},
        )


# ---------------------------------------------------------------------------
# service registration
# ---------------------------------------------------------------------------


async def test_an_account_alone_registers_only_update_contents(hass):
    account = await _setup_account(hass)

    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")
    assert not hass.services.has_service(DOMAIN, "untrack_parcel")

    assert await hass.config_entries.async_unload(account.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_unloading_one_entry_keeps_the_services_the_others_need(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)
    assert hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(hub.entry_id)
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(account.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_a_hub_after_an_account_still_registers_the_hub_services(hass):
    await _setup_account(hass)
    await _setup(hass, [HUB_CODE])

    assert hass.services.has_service(DOMAIN, "track_parcel")
    assert hass.services.has_service(DOMAIN, "untrack_parcel")


# ---------------------------------------------------------------------------
# a contents change never polls the carrier
# ---------------------------------------------------------------------------


async def test_account_update_contents_reapplies_without_a_poll(hass):
    account = await _setup_account(hass)
    with _no_poll() as (refresh, fetch), _listener_count(account) as count:
        await _quiet_call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACCOUNT_CODE, "contents": "Running shoes"},
        )
        assert count() == 1
        await _quiet_call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACCOUNT_CODE, "contents": "Running shoes"},
        )
        await _quiet_call(hass, "update_contents", {CONF_TRACKING_CODE: UNTRACKED})
        assert count() == 1
    assert _contents(account, ACCOUNT_CODE) == "Running shoes"
    refresh.assert_not_called()
    fetch.assert_not_called()


async def test_clearing_a_code_no_entry_lists_notifies_nobody(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)
    with _no_poll() as (refresh, _fetch), _listener_count(hub, account) as count:
        await _quiet_call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: UNTRACKED, "config_entry_id": hub.entry_id},
        )
        assert count() == 0
    refresh.assert_not_called()


async def test_two_entries_listing_a_code_are_both_reapplied_without_a_poll(hass):
    home = await _setup(hass, [HUB_CODE])
    work = await _setup(hass, [HUB_CODE], country="PT")
    with _no_poll() as (refresh, fetch), _listener_count(home, work) as count:
        await _quiet_call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
        )
        assert count() == 2
    assert _contents(home, HUB_CODE) == "Running shoes"
    assert _contents(work, HUB_CODE) == "Running shoes"
    refresh.assert_not_called()
    fetch.assert_not_called()


async def test_a_barcode_less_parcel_keeps_its_contents_after_reapply_and_poll(hass):
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    real = module.normalize_tracking_parcel

    def no_barcode(raw, **kwargs):
        parcel = real(raw, **kwargs)
        parcel["barcode"] = None
        return parcel

    with (
        patch.object(module, "normalize_tracking_parcel", side_effect=no_barcode),
        patch(
            f"{HUB_CLIENT}.async_get_parcel",
            new=AsyncMock(return_value=_hub_payload(HUB_CODE)),
        ),
    ):
        await coordinator.async_refresh()
        coordinator.contents_store.async_set(HUB_CODE, "Running shoes")
        coordinator.async_reapply_contents()
        assert coordinator.data[0]["contents"] == "Running shoes"
        await coordinator.async_refresh()
        assert coordinator.data[0]["contents"] == "Running shoes"


async def test_reapply_keeps_the_text_when_the_barcode_differs_from_the_code(hass):
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    with patch(
        f"{HUB_CLIENT}.async_get_parcel",
        new=AsyncMock(return_value=_hub_payload(DELIVERED_CODE)),
    ):
        await coordinator.async_refresh()
    coordinator.contents_store.async_set(HUB_CODE, "Running shoes")
    coordinator.async_reapply_contents()
    assert coordinator.data[0]["barcode"] == DELIVERED_CODE
    assert coordinator.data[0]["contents"] == "Running shoes"
    coordinator.contents_store.async_set(HUB_CODE, None)
    coordinator.async_reapply_contents()
    assert coordinator.data[0]["contents"] is None


async def test_reapply_reaches_delivered_hub_parcels(hass):
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    with patch(
        f"{HUB_CLIENT}.async_get_parcel",
        new=AsyncMock(return_value=_hub_payload(HUB_CODE, status="EOL.1001")),
    ):
        await coordinator.async_refresh()
    assert coordinator.delivered
    coordinator.contents_store.async_set(HUB_CODE, "Winter socks")
    coordinator.async_reapply_contents()
    assert coordinator.delivered[0]["contents"] == "Winter socks"


async def test_reapply_reaches_active_and_delivered_account_parcels(hass):
    account = await _setup_account(hass)
    coordinator = account.runtime_data.coordinator
    coordinator.contents_store.async_set(ACCOUNT_CODE, "Running shoes")
    coordinator.contents_store.async_set(DELIVERED_CODE, "Winter socks")
    coordinator.async_reapply_contents()
    assert coordinator.data[0]["contents"] == "Running shoes"
    assert coordinator.delivered[0]["contents"] == "Winter socks"
