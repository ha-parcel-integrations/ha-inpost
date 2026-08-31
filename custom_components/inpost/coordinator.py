"""Coordinator for the InPost parcel tracker integration.

Fetching and event firing only — the parcel mapping lives in :mod:`.parcels`.
One authenticated call to the inbox per poll; the client refreshes the access
token under the hood, and only a dead session (refresh failed) surfaces here as
a reauth. InPost exposes no delivery ETA, so the delivery-time event never
fires — it is kept for contract parity, harmlessly inert.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    InPostApiClient,
    InPostApiError,
    InPostAuthReauthRequired,
    InPostTrackingApiClient,
)
from .const import (
    CONF_COUNTRY,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    HOT_LOOKAHEAD_HOURS,
    MID_INTERVAL_MINUTES,
    QUIET_WINDOW_END_HOUR,
    QUIET_WINDOW_START_HOUR,
    STAGGER_MINUTES,
    ParcelStatus,
)
from .parcels import (
    apply_delivered_filter,
    normalize_parcel,
    normalize_tracking_parcel,
    sort_parcels_by_ts,
)

_LOGGER = logging.getLogger(__name__)
_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 3600


def _stagger_minutes(entry_id: str) -> int:
    """Return a stable, per-install schedule offset."""
    return int(hashlib.sha256(entry_id.encode()).hexdigest(), 16) % STAGGER_MINUTES


def _in_quiet_window(moment: datetime) -> bool:
    """Whether local ``moment`` is inside the no-polling window."""
    return QUIET_WINDOW_START_HOUR <= moment.hour < QUIET_WINDOW_END_HOUR


def _next_anchor(now: datetime) -> datetime:
    """Return the next local 00:00 or 06:00 polling anchor."""
    six_today = now.replace(
        hour=QUIET_WINDOW_END_HOUR, minute=0, second=0, microsecond=0
    )
    if now < six_today:
        return six_today
    return (now + timedelta(days=1)).replace(
        hour=QUIET_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )


def _hottest_tier_minutes(
    active_parcels: list[dict], now: datetime, *, stop_when_empty: bool
) -> int | None:
    """Return hot/mid tier, or suspend a code-based hub with no active codes."""
    if not active_parcels:
        return None if stop_when_empty else MID_INTERVAL_MINUTES
    for parcel in active_parcels:
        if parcel["status"] != ParcelStatus.OUT_FOR_DELIVERY:
            continue
        planned_from = parcel.get("planned_from")
        planned_dt = dt_util.parse_datetime(planned_from) if planned_from else None
        if planned_dt is None or dt_util.as_utc(now) >= dt_util.as_utc(
            planned_dt
        ) - timedelta(hours=HOT_LOOKAHEAD_HOURS):
            return HOT_INTERVAL_MINUTES
    return MID_INTERVAL_MINUTES


def _next_update_interval(
    now: datetime, tier_minutes: int | None, entry_id: str
) -> timedelta | None:
    """Convert a tier to a local-time-aware next coordinator interval."""
    if tier_minutes is None:
        return None
    if _in_quiet_window(now):
        return _next_anchor(now) - now
    candidate = now + timedelta(
        minutes=tier_minutes + _stagger_minutes(entry_id)
    )
    if _in_quiet_window(candidate):
        return _next_anchor(now) - now
    return candidate - now


class InPostCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls the account's parcel list and publishes the canonical lists.

    ``coordinator.data`` is the active (not-yet-delivered) parcels,
    ``self.delivered`` the rest.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: InPostApiClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Passing config_entry makes self.config_entry available on the
            # base class, which every helper below relies on.
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=HOT_INTERVAL_MINUTES),
        )
        self._client = client
        self.delivered: list[dict] = []
        # barcode -> last seen ParcelStatus / (planned_from, planned_to).
        # ``None`` on the first refresh so events are suppressed for parcels
        # that already existed when the integration started — otherwise every
        # restart would flood users with "registered" notifications.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        # Cached device id, attached to every fired event so device-trigger
        # automations can filter to this account's device.
        self._cached_device_id: str | None = None
        # Timestamp of the last successful poll (diagnostic sensor).
        self.last_success_time: datetime | None = None
        self._current_tier_minutes: int | None = None
        self._consecutive_429 = 0

    @property
    def current_tier_minutes(self) -> int | None:
        """Last dynamic tier, for diagnostics."""
        return self._current_tier_minutes

    def _set_dynamic_interval(
        self, active_parcels: list[dict], *, stop_when_empty: bool
    ) -> None:
        """Recompute the next schedule after a successful refresh."""
        now = dt_util.now()
        self._current_tier_minutes = _hottest_tier_minutes(
            active_parcels, now, stop_when_empty=stop_when_empty
        )
        self.update_interval = _next_update_interval(
            now, self._current_tier_minutes, self.config_entry.entry_id
        )

    def _handle_api_error(self, err: InPostApiError) -> None:
        """Turn any API failure into HA's native retry, never an unhandled crash.

        ``DataUpdateCoordinator`` only treats :class:`UpdateFailed` as a normal,
        retryable failure — anything else falls into its bare ``except
        Exception`` branch, which logs "Unexpected error fetching data" with a
        full traceback and skips the usual backoff. A plain (non-429) upstream
        error, e.g. a transient HTTP 500 from ``inposteasy.com``, is exactly as
        retryable as a 429 and must not surface as if it were a bug here.
        """
        if err.status_code != 429:
            raise UpdateFailed(str(err)) from err
        self._consecutive_429 += 1
        retry_after = err.retry_after or min(
            _BACKOFF_BASE_SECONDS * 2**self._consecutive_429,
            _BACKOFF_CAP_SECONDS,
        )
        raise UpdateFailed("InPost rate-limited (429)", retry_after=retry_after)

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
        )

    async def _async_update_data(self) -> list[dict]:
        """Fetch the account's parcels and split into active vs delivered.

        ``aiohttp.ClientError`` and ``InPostApiError`` are deliberately not
        caught — ``DataUpdateCoordinator`` turns them into ``UpdateFailed`` with
        backoff. Only a dead session (the token refresh itself failed) needs
        special handling, because retrying it forever would never recover.
        """
        try:
            raws = await self._client.async_get_parcels()
        except InPostAuthReauthRequired as err:
            raise ConfigEntryAuthFailed("InPost session expired") from err
        except InPostApiError as err:
            self._handle_api_error(err)

        include_history = self._include_history
        normalized = [
            normalize_parcel(raw, include_history=include_history) for raw in raws
        ]
        active = [parcel for parcel in normalized if not parcel["delivered"]]
        delivered = [parcel for parcel in normalized if parcel["delivered"]]

        self.delivered = apply_delivered_filter(
            sort_parcels_by_ts(delivered, "delivered_at", descending=True),
            self.config_entry,
        )
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        # Incoming = active + delivered, combined so the transition to
        # delivered is visible in one set.
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
        self._set_dynamic_interval(normalized_active, stop_when_empty=False)
        return normalized_active

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire registered / status-changed / delivered / delivery-time events.

        Silent on the very first refresh — we cannot know which parcels are
        genuinely new versus already present before HA started.

        The event contract, identical across the suite:

        * every payload is the full normalised parcel plus ``device_id``;
        * the hop **to** ``delivered`` fires only ``_parcel_delivered``, never
          also ``_parcel_status_changed``;
        * a barcode first seen already-delivered fires nothing;
        * ``registered`` only fires for a new, not-yet-delivered barcode;
        * an ETA going ``value → null`` is intentionally silent — the carrier
          just lost the window, which is not worth waking someone up for.
        """
        if self._known_state is None:
            return

        known_times = self._known_delivery_times or {}
        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            new_status = parcel["status"]
            if barcode not in self._known_state:
                if new_status != ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_registered",
                        {**parcel, "device_id": device_id},
                    )
                continue

            if self._known_state[barcode] != new_status:
                if new_status == ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_delivered",
                        {**parcel, "device_id": device_id},
                    )
                else:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_status_changed",
                        {
                            **parcel,
                            "device_id": device_id,
                            "old_status": self._known_state[barcode],
                            "new_status": new_status,
                        },
                    )

            old_from, old_to = known_times.get(barcode, (None, None))
            new_from = parcel.get("planned_from")
            new_to = parcel.get("planned_to")
            from_changed = new_from is not None and new_from != old_from
            to_changed = new_to is not None and new_to != old_to
            if from_changed or to_changed:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_delivery_time_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_planned_from": old_from,
                        "new_planned_from": new_from,
                        "old_planned_to": old_to,
                        "new_planned_to": new_to,
                    },
                )


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

    async def _async_update_data(self) -> list[dict]:
        codes = [
            item.get(CONF_TRACKING_CODE)
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if isinstance(item, dict) and item.get(CONF_TRACKING_CODE)
        ]
        try:
            raws = await asyncio.gather(
                *(self._tracking_client.async_get_parcel(code) for code in codes)
            )
        except InPostApiError as err:
            self._handle_api_error(err)
        normalized = [
            normalize_tracking_parcel(
                raw, country=self._country, include_history=self._include_history
            )
            for raw in raws
        ]
        active = [parcel for parcel in normalized if not parcel["delivered"]]
        delivered = [parcel for parcel in normalized if parcel["delivered"]]
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
