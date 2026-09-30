# Working in this repository

Home Assistant custom integration for **InPost** (Paczkomat locker network) parcel
tracking. Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo. No DTO
layer.

**Two structurally independent backends, one repo.** InPost started as the
suite's first **account-based** carrier with an SMS login (Poland only;
auto-imports the account's parcels, no manual services); new setups now sign
in through the InPost Group sign-in page instead, for a Polish or an Italian
account. It has since grown a second,
**keyless public-tracking** model (`PL`/`IT`/`PT`/`GB`/`ES`) for barcode-only setup:
no login, one config entry per country, parcels added/removed via the
`track_parcel`/`untrack_parcel` services. The two live side by side rather than
converging into one coordinator/capability shape the way `ha-gls`/`ha-dpd`
converge same-model countries — deliberately, because the auth models
themselves differ in kind (a signed-in token pair vs. keyless GET), not just the data
depth. See `CAPABILITIES_BY_VARIANT` in `const.py` for the capability sets
(Polish account, Italian account, tracking).

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

**API mechanics live in `carrier-research/inpost/api/` (private research repo)** — the group
sign-in and the legacy SMS auth, both token-refresh endpoints, the
`/v3/parcels/tracked` list, the two `Authorization` header shapes, and the
two-tier status vocabulary. Do not duplicate them
here.

**Structure, options flow, dynamic polling and module layout are suite-wide**
and identical in every carrier — the authoritative spec is
[`ha-carrier-template/scaffold/CLAUDE.md`](https://github.com/ha-parcel-integrations/ha-carrier-template/blob/main/scaffold/CLAUDE.md).
Where this repo diverges from it, that is recorded below under
*Divergences from the scaffold*.

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

InPost is the Paczkomat locker network. First account-based carrier with an
SMS login and first with a real "waiting in a locker" state. The account
backend serves Poland and Italy, on two different parcel backends behind one
sign-in (see *Market* below). Public tracking (`PL`/`IT`/`PT`/`GB`/`ES`)
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

- **Sign-in: the InPost Group page, pasted back.** The login page needs a
  captcha and redirects only to an InPost-owned callback, so the config flow
  shows a PKCE sign-in link (`account/oauth.py`), the user signs in in their
  own browser and pastes the callback URL back; it is validated for
  host/path/state before the code is ever exchanged. Setup asks for the
  **country first** (`account` step), then shows the link (`sign_in`); reauth
  reuses the entry's country. The same link is kept for the whole flow, so a
  retry does not invalidate a link already opened.
- **Market: `CONF_MARKET` picks the parcel backend.** The token's `market`
  claim decides which backend an account lives on, and the two refuse each
  other's tokens: `PL` reads the legacy inbox (`/v3/parcels/tracked`,
  `account/parcels.py`); `IT` reads the group backend's paged list
  (`account/countries/it.py`, its own normaliser). Entries without the key are
  Polish. A token whose market differs from the chosen country is refused as
  `wrong_market` — in practice a browser still signed in to another account.
- **Italy is built without a real parcel.** Sign-in and the (empty) parcel
  list are live-confirmed; the per-parcel shape is modelled on the app. The
  status comes from InPost's own published catalogue (8 codes, complete), the
  current status is the newest event's, and a payload that differs from the
  model warns once. `weight`/`dimensions` stay `None` and `Account (IT)`
  claims only `url`/`history` until a real parcel settles units and the
  pickup-point shape.
- **Two token kinds, one inbox — `CONF_AUTH_METHOD` picks.** `sso` entries hold
  the sign-in's OAuth pair: `Bearer` header, refreshed at the sign-in's token
  endpoint (a 400/401 there is a dead session). Entries without the key are
  from the SMS era and keep the app's own pair: bare header, refreshed at
  `/v1/authenticate`. **Existing SMS entries are never forced over**; their next
  reauth moves them to `sso` in place (same entry, same entities). Do not add a
  startup migration or bring the SMS login steps back.
- **`unique_id` is the token's `phone` claim**, the bare national number
  without dial code, as SMS entries were keyed, so the same account cannot be
  added twice across the two token kinds. Polish (9 digits) and Italian (10)
  numbers cannot collide, so no prefix is needed. A reauth that signs in to another phone aborts `wrong_account`
  instead of rebinding the entry. Do not switch it to `sub`.
- **Token handling (do not weaken).** Tokens live in `entry.data` (never options,
  never diagnostics). A 401 triggers one refresh + retry; a *failed* refresh →
  `InPostAuthReauthRequired` → `ConfigEntryAuthFailed` → reauth; a refresh
  *transport* error, 429 or 5xx stays `InPostApiError` → retry. Rotated tokens
  are persisted via `on_tokens_updated`. Neither kind reliably rotates its
  refresh token (the app's refresh returns `authToken` alone, the sign-in's has
  been seen returning the same refresh token): keep the stored one when none
  comes back, persist a new one when it does. Access tokens live ~2 h, so the
  refresh path runs on every poll cycle past that.
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

## Divergences from the scaffold

Everything not listed here follows the scaffold exactly.

*Two entry types, two options models — do not swap them.* The **account hub**
uses one sectioned form calling `async_schedule_reload` with no update
listener. A **tracking hub** instead registers
`_async_tracking_options_updated` as an update listener and applies a code
add/remove immediately via `async_request_refresh` — no reload, since adding a
barcode has nothing to re-authenticate.

*Dynamic polling* — both coordinators run it **unconditionally**: no
user-facing interval option, `auto` is not a choice but the only mode. InPost
converged straight to that shape by deliberate choice, ahead of the suite-wide
convergence that finished on 2026-09-12; an entry upgrading from the old
numeric `refresh_interval` has that option silently dropped rather than
preserved. The account hub **never stops**
(`stop_when_empty=False` — an account can gain a parcel between polls with
nothing to trigger a refresh); a tracking hub suspends entirely with no tracked
codes (`stop_when_empty=True`).

*Module layout: two sources, two subpackages.* This follows the suite's
multi-source convention (the same shape as `ha-bpost` and `ha-dhl`). Each source
owns its client, coordinator, normaliser and status map under
`custom_components/inpost/<source>/`:

| Module | Holds |
|---|---|
| `account/client.py` | `async_exchange_code`, `InPostApiClient` (both token kinds, refresh, parcel list), `InPostApiError`/`InPostAuthReauthRequired`, the inbox host URLs |
| `account/oauth.py` | Pure sign-in helpers: PKCE, the authorization URL, callback validation, token-claim decoding |
| `account/countries/it.py` | The Italian backend: its paged parcel URL, catalogue status map and normaliser |
| `account/coordinator.py` | `InPostCoordinator` and the dynamic-polling helpers both sources use |
| `account/parcels.py` | `normalize_parcel`, `STATUS_MAP` + `STATUS_GROUP_MAP`, and the suite-wide helpers (`parse_iso`, sort, delivered filter, `NEW_ISSUE_URL`) |
| `tracking/client.py` | `InPostTrackingApiClient`, `EASY_TRACKING_URL` |
| `tracking/coordinator.py` | `InPostTrackingCoordinator(InPostCoordinator)` |
| `tracking/parcels.py` | `normalize_tracking_parcel`, `TRACKING_STATUS_MAP` |
| `const.py` | Shared keys and defaults, `TRACKING_URL_BY_COUNTRY` (tracking hubs and Italian accounts), and **`CAPABILITIES_BY_VARIANT`, which must stay here**: the docs site reads it from this file |
| `config_flow.py`, `services.py` (tracking hubs only), `diagnostics.py` (`TO_REDACT` incl. `qrCode`/`openCode`), platforms | Root. They are shared across sources |

`tracking/` imports shared pieces from `account/`, one way only. `api.py`,
`coordinator.py` and `parcels.py` at the root are **re-export shims** for the
pre-split import paths. Nothing in the repo imports through them;
`tests/test_compat_imports.py` guards them. Tests mirror the split
(`tests/account/`, `tests/tracking/`). The source-agnostic platform tests,
`test_polling.py`, the config flow and the shared `payloads.py` stay top-level.

`__init__.py` branches once, on `CONF_COUNTRY in entry.data`, to pick the
account vs tracking wiring.

## Running tests

```
python -m pytest tests/ --cov=custom_components.inpost
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file in the same commit;
the API reference now lives in the private `carrier-research/inpost/api/`,
not in this repo.
