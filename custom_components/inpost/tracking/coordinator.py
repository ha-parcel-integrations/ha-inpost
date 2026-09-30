"""Coordinator for a public-tracking hub: one country, explicit codes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..account.coordinator import InPostCoordinator
from ..account.parcels import apply_delivered_filter, sort_parcels_by_ts
from ..const import CONF_COUNTRY, CONF_PARCELS, CONF_TRACKING_CODE
from .client import InPostApiError, InPostTrackingApiClient
from .parcels import normalize_tracking_parcel


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

    async def _async_update_data(self) -> list[dict]:
        codes = [
            item.get(CONF_TRACKING_CODE)
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

        try:
            fetched = await asyncio.gather(
                *(self._tracking_client.async_get_parcel(code) for code in codes_to_fetch)
            )
        except InPostApiError as err:
            self._handle_api_error(err)

        raws_by_code: dict[str, dict] = dict(zip(codes_to_fetch, fetched))
        for code, raw in raws_by_code.items():
            self._raw_cache[code] = raw

        # Codes skipped from the fetch above (already confirmed delivered) —
        # re-add their cached payload so the delivered sensor keeps its data
        # until the retention filter drops it.
        for code in self._delivered_codes:
            cached = self._raw_cache.get(code)
            if cached is not None:
                raws_by_code[code] = cached

        entries = [
            (
                code,
                normalize_tracking_parcel(
                    raw, country=self._country, include_history=self._include_history
                ),
            )
            for code, raw in raws_by_code.items()
        ]
        active = [parcel for _, parcel in entries if not parcel["delivered"]]
        delivered = [parcel for _, parcel in entries if parcel["delivered"]]
        # Rebuilt fresh from this cycle's data — a code whose payload just
        # flipped to delivered is skipped starting next cycle; one that
        # somehow un-delivers (should not happen, but the fetch list must
        # never permanently drop a code) rejoins it automatically.
        self._delivered_codes = {code for code, parcel in entries if parcel["delivered"]}
        self.delivered = apply_delivered_filter(
            sort_parcels_by_ts(delivered, "delivered_at", descending=True),
            self.config_entry,
        )
        normalized_active = sort_parcels_by_ts(active, "planned_from")
        incoming = normalized_active + self.delivered
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
        self.last_success_time = datetime.now(timezone.utc)
        self._consecutive_429 = 0
        self._set_dynamic_interval(normalized_active, stop_when_empty=True)
        return normalized_active
