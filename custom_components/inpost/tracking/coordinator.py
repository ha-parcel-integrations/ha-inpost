"""Coordinator for a public-tracking hub: one country, explicit codes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from ..account.coordinator import InPostCoordinator
from ..account.parcels import apply_delivered_filter, sort_parcels_by_ts
from ..const import CONF_COUNTRY, CONF_PARCELS, CONF_TRACKING_CODE
from ..contents import normalize_code
from .client import InPostApiError, InPostTrackingApiClient
from .parcels import normalize_tracking_parcel

_LOGGER = logging.getLogger(__name__)


class InPostTrackingCoordinator(InPostCoordinator):
    """Poll the explicitly configured public tracking codes for one country."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: InPostTrackingApiClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the public-tracking coordinator."""
        # The base class owns the suite-standard event and filtering behaviour.
        super().__init__(hass, client, entry)  # type: ignore[arg-type]
        self._tracking_client = client
        self._country = entry.data.get(CONF_COUNTRY)
        # tracking_code -> last successful raw payload, so a delivered code
        # skipped from the fetch (below) still has something to normalize.
        self._raw_cache: dict[str, dict] = {}

    async def _async_poll(self) -> list[dict]:
        codes: list[str] = [
            item[CONF_TRACKING_CODE]
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if isinstance(item, dict) and item.get(CONF_TRACKING_CODE)
        ]

        # Drop cache entries for codes the user no longer follows, so the
        # cache stays bounded.
        tracked_codes = set(codes)
        self._raw_cache = {
            code: raw for code, raw in self._raw_cache.items() if code in tracked_codes
        }
        self._delivered_codes &= tracked_codes

        # A delivered parcel's payload can never change again, so it is
        # dropped from the fetch — not from ``codes``/the options list, which
        # stays untouched until the user removes it by hand.
        codes_to_fetch = [code for code in codes if code not in self._delivered_codes]

        fetched = await asyncio.gather(
            *(self._tracking_client.async_get_parcel(code) for code in codes_to_fetch),
            return_exceptions=True,
        )

        # One code failing must not take the whole hub down: it keeps its
        # last good payload and the others still update.
        raws_by_code: dict[str, dict] = {}
        failures: list[InPostApiError] = []
        unresolved: list[str] = []
        for code, result in zip(codes_to_fetch, fetched, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(
                    result, (InPostApiError, aiohttp.ClientError, TimeoutError)
                ):
                    raise result
                _LOGGER.warning("InPost tracking failed for %s: %s", code, result)
                err = (
                    result
                    if isinstance(result, InPostApiError)
                    else InPostApiError(str(result))
                )
                failures.append(err)
                cached = self._raw_cache.get(code)
                if cached is not None:
                    raws_by_code[code] = cached
                elif err.status_code is not None:
                    # InPost answered, so the host is up and only this code
                    # has nothing to show: it still counts, as unknown.
                    unresolved.append(code)
                continue
            self._raw_cache[code] = result
            raws_by_code[code] = result

        # A 429 on any code backs the whole poll off: it is one shared host.
        rate_limited = next((e for e in failures if e.status_code == 429), None)
        if rate_limited is not None:
            self._handle_api_error(rate_limited)

        # Codes skipped from the fetch above (already confirmed delivered) —
        # re-add their cached payload so the delivered sensor keeps its data
        # until the retention filter drops it.
        for code in self._delivered_codes:
            cached = self._raw_cache.get(code)
            if cached is not None:
                raws_by_code[code] = cached

        if (
            failures
            and len(failures) == len(codes_to_fetch)
            and not raws_by_code
            and not unresolved
        ):
            raise UpdateFailed(
                f"InPost tracking failed for every parcel: {failures[0]}"
            )
        for code in unresolved:
            raws_by_code[code] = {"trackingNumber": code}

        entries = [
            (
                code,
                self._apply_contents(
                    normalize_tracking_parcel(
                        raw,
                        country=self._country,
                        include_history=self._include_history,
                    ),
                    code,
                ),
            )
            for code, raw in raws_by_code.items()
        ]
        self._parcel_codes = {id(parcel): code for code, parcel in entries}
        self.known_codes = frozenset(
            normalize_code(value)
            for code, parcel in entries
            for value in (code, parcel.get("barcode"))
            if value
        )
        active = [parcel for _, parcel in entries if not parcel["delivered"]]
        delivered = [parcel for _, parcel in entries if parcel["delivered"]]
        # Rebuilt fresh from this cycle's data — a code whose payload just
        # flipped to delivered is skipped starting next cycle; one that
        # somehow un-delivers (should not happen, but the fetch list must
        # never permanently drop a code) rejoins it automatically.
        self._delivered_codes = {
            code for code, parcel in entries if parcel["delivered"]
        }
        delivered_sorted = sort_parcels_by_ts(
            delivered, "delivered_at", descending=True
        )
        self.delivered = apply_delivered_filter(delivered_sorted, self.config_entry)
        normalized_active = sort_parcels_by_ts(active, "planned_from")
        # Events and remembered state cover every parcel; the retention option
        # only limits what the delivered sensor shows.
        incoming = normalized_active + delivered_sorted
        self._fire_change_events(incoming)
        self._known_state = {
            parcel["barcode"]: parcel["status"]
            for parcel in incoming
            if parcel.get("barcode")
        }
        self._known_delivery_times = {
            parcel["barcode"]: (parcel.get("planned_from"), parcel.get("planned_to"))
            for parcel in incoming
            if parcel.get("barcode")
        }
        self.last_success_time = datetime.now(UTC)
        self._consecutive_429 = 0
        self._set_dynamic_interval(normalized_active, stop_when_empty=True)
        return normalized_active
