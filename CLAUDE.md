# Working in this repository

Home Assistant custom integration for **InPost** (Paczkomat locker network) parcel
tracking. Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo. The
suite's first **account-based, SMS-login** carrier (auto-imports the account's
parcels; no manual services). No DTO layer.

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

**API mechanics live in `carrier-research/api/inpost/` (private research repo)** — the SMS auth
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

InPost is the Paczkomat locker network (Poland, growing in Italy). First
account-based SMS-login carrier and first with a real "waiting in a locker" state.
Mapping is well-evidenced but **not exercised against an account we control**
(pre-1.0).

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
- **QR / openCode redaction (do not weaken)** — the locker-opening codes stay under
  `raw` and are **redacted in diagnostics**; a live `openCode` is a
  physical-security leak. A QR `image` entity is a possible fast-follow, out of
  scope for now.

## Options and reloads — account-based model

The options flow is one sectioned form. **Account-based**, so it calls
`async_schedule_reload` on submit and registers **no** update listener (combining a
listener with a reload-on-update flow is deprecated, error in HA 2026.12+). The
user-tunable poll interval is a deliberate HACS divergence (see CONVENTIONS.md).

## Module layout

| File | Carrier-specific? |
|---|---|
| `api.py` (SMS auth, token refresh, parcel list, error types) | **yes** |
| `const.py` (domain, URLs, `ParcelStatus`, status maps, option keys) | partly (URLs, maps) |
| `parcels.py` (status map, `normalize_parcel`, history, sort, filters — pure, no I/O) | partly (`_STATUS_MAP`, `normalize_parcel`) |
| `coordinator.py` (fetch, cache, event firing) | mostly not |
| `config_flow.py` (2-step phone→SMS, reauth, options) | partly |
| `sensor.py` / `button.py` / `calendar.py` / `device_trigger.py` | no |
| `diagnostics.py` | partly (`TO_REDACT`, incl. `qrCode`/`openCode`) |

No manual services — the account auto-imports parcels. `parcels.py` is free of I/O
and HA objects so the per-carrier part stays unit-testable. Config:
`ConfigEntry.runtime_data` (typed, no `hass.data`), `PARALLEL_UPDATES = 0`,
coordinator takes `config_entry=entry`. `aiohttp.ClientError` is caught **per
parcel** in the gather loop (one bad parcel doesn't fail the poll) but **not**
around the whole update (coordinator wraps that). Entities: `has_entity_name` +
`translation_key`, `icons.json`, translated units, `_attr_attribution`,
`_unrecorded_attributes` on anything with a parcel list or `raw`. Over-redact
diagnostics.

## Tests on Windows

`tests/conftest.py` carries two Windows-only shims (no-ops elsewhere):
`disable_socket` is neutralised (Windows event loops need AF_INET socketpairs;
the 127.0.0.1 allowlist stays) and HA's `AsyncResolver` is swapped for
`ThreadedResolver` (aiodns refuses the Proactor loop). Do not remove them
"because CI passes" — CI is Linux, development is Windows.

## Running tests

```
python -m pytest tests/ --cov=custom_components.inpost
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file in the same commit;
the API reference now lives in the private `carrier-research/api/inpost/`,
not in this repo.
