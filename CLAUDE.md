# Working in this repository

Home Assistant custom integration for **InPost** (Paczkomat locker network) parcel
tracking. Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo. No DTO
layer.

**Two structurally independent backends, one repo.** InPost started as the
suite's first **account-based, SMS-login** carrier (Poland only; auto-imports
the account's parcels, no manual services). It has since grown a second,
**keyless public-tracking** model (`PL`/`IT`/`PT`/`GB`) for barcode-only setup:
no login, one config entry per country, parcels added/removed via the
`track_parcel`/`untrack_parcel` services. The two live side by side rather than
converging into one coordinator/capability shape the way `ha-gls`/`ha-dpd`
converge same-model countries — deliberately, because the auth models
themselves differ in kind (SMS token dance vs. keyless GET), not just the data
depth. See `CAPABILITIES_BY_VARIANT` in `const.py` for the two capability sets.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| ship anything while below 1.0.0 (mapping not run against a real account) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client) | *Deliberate skill divergences* |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**API mechanics live in `carrier-research/inpost/api/` (private research repo)** — the SMS auth
flow, the token-refresh endpoints, the `/v3/parcels/tracked` list, the bare
`Authorization` header, and the two-tier status vocabulary. Do not duplicate them
here.

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).

## Carrier-specific decisions (integration only)

InPost is the Paczkomat locker network. First account-based SMS-login carrier
and first with a real "waiting in a locker" state — that part stays Poland-only
(the account API has no other-country variant). Public tracking (`PL`/`IT`/`PT`/`GB`)
is a separate, later addition; see the backend split at the top of this file.
**Confirmed against a real account 2026-08-15** — auth, the parcel-list
endpoint and the happy path all round-tripped correctly; the fuller detailed
status list (~60 documented values, one live-confirmed so far) is still
growing, degrading safely to the coarse `statusGroup` + a one-shot warning.
That real payload also caught two shape bugs: history came from `eventLog`
(`{type, name, date}`), not the assumed `events`/`eventTitle`, and its `name`
turned out to share the exact same status vocabulary as the top-level
`status` field, so history entries now carry a mapped `status` too, not just
free text.

- **Token handling (do not weaken).** Tokens live in `entry.data` (never options,
  never diagnostics). A 401 triggers one refresh + retry; a *failed* refresh →
  `InPostAuthReauthRequired` → `ConfigEntryAuthFailed` → SMS reauth; a refresh
  *transport* error stays `InPostApiError` → retry. Rotated tokens are persisted
  via `on_tokens_updated`. The config flow is **two-step** (phone → SMS code) for
  setup and reauth; reauth fixes the phone to the entry's, so it can't rebind to
  another account.
- **Status strategy**: the detailed status maps first, then falls back to the
  coarse status group, so an unmapped detailed value still buckets sensibly (and
  still warns). `operations.collect == true` forces `pickup: true` even for an
  unbucketed status string. A locker "signing" is **not** a delivery
  (`at_pickup_point`); `claimed` is terminal picked-up.
- **No ETA** — `planned_from`/`planned_to` always `None` (calendar and
  `next_delivery` inert). No weight/dimensions (only a size class, under `raw`).
- **An unmapped history status stays `None`, never `ParcelStatus.UNKNOWN`** — in
  both `build_history()` (account backend) and `normalize_tracking_parcel()`
  (public-tracking backend), so `None` distinguishes "no status known for this
  event" from a parcel whose *current* status is genuinely `unknown`. Matches
  `ha-dhl-nl`'s precedent; keep the two normalizers aligned on this.
- **QR / openCode redaction (do not weaken)** — the locker-opening codes stay under
  `raw` and are **redacted in diagnostics**; a live `openCode` is a
  physical-security leak. A QR `image` entity is a possible fast-follow, out of
  scope for now.

## Options and reloads

The account hub's options flow is one sectioned form; it calls
`async_schedule_reload` on submit and registers **no** update listener (combining
a listener with a reload-on-update flow is deprecated, error in HA 2026.12+). A
tracking hub's options flow instead registers `_async_tracking_options_updated`
as an update listener and applies a code addition/removal immediately via
`async_request_refresh` — no reload, since adding a barcode has nothing to
re-authenticate. Do not swap these two patterns between the entry types.

## Dynamic polling

Both coordinators run the suite's dynamic, status-driven polling
(`carrier-research/dynamic-polling.md`) **unconditionally** — no user-facing
interval option, `auto` is not a choice, it is the only mode. This is ahead of
where most of the suite currently sits (`ha-postnl`/`ha-gls` still expose the
Phase 1 hybrid dropdown); InPost converged straight to the final shape by
deliberate choice, and a config entry upgrading from the old numeric
`refresh_interval` has that option silently dropped rather than preserved.
The account hub never stops polling (`stop_when_empty=False` — an account can
gain a new parcel between polls with nothing to trigger a refresh); a tracking
hub suspends polling entirely when it has no tracked codes
(`stop_when_empty=True`).

## Module layout

| File | Carrier-specific? |
|---|---|
| `api.py` (SMS auth, token refresh, parcel list, `InPostTrackingApiClient` for the keyless per-country endpoint, error types) | **yes** |
| `const.py` (domain, URLs, `ParcelStatus`, status maps, `TRACKING_URL_BY_COUNTRY`, `CAPABILITIES_BY_VARIANT`, option keys) | partly (URLs, maps) |
| `parcels.py` (`normalize_parcel` for the account inbox, `normalize_tracking_parcel` for public tracking — two independent pure functions, no I/O) | partly (`STATUS_MAP`, `TRACKING_STATUS_MAP`) |
| `coordinator.py` (`InPostCoordinator` + `InPostTrackingCoordinator(InPostCoordinator)`; fetch, cache, event firing) | mostly not |
| `config_flow.py` (menu picks account vs. tracking; 2-step phone→SMS for the account, country + codes for tracking; reauth, options) | partly |
| `services.py` / `services.yaml` (`track_parcel` / `untrack_parcel`, tracking hubs only) | **yes** |
| `sensor.py` / `button.py` / `calendar.py` / `device_trigger.py` | no |
| `diagnostics.py` | partly (`TO_REDACT`, incl. `qrCode`/`openCode`) |

`__init__.py` branches once, on `CONF_COUNTRY in entry.data`, to pick the
account vs. tracking client/coordinator/services wiring — see the backend
split at the top of this file. `parcels.py` is free of I/O
and HA objects so the per-carrier part stays unit-testable. Config:
`ConfigEntry.runtime_data` (typed, no `hass.data`), `PARALLEL_UPDATES = 0`,
coordinator takes `config_entry=entry`. `aiohttp.ClientError` is caught **per
parcel** in the gather loop (one bad parcel doesn't fail the poll) but **not**
around the whole update (coordinator wraps that). Entities: `has_entity_name` +
`translation_key`, `icons.json`, translated units, `_attr_attribution`,
`_unrecorded_attributes` on anything with a parcel list or `raw`. Over-redact
diagnostics.

## Running tests

```
python -m pytest tests/ --cov=custom_components.inpost
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file in the same commit;
the API reference now lives in the private `carrier-research/inpost/api/`,
not in this repo.
