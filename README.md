# InPost Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-inpost.svg)](https://github.com/ha-parcel-integrations/ha-inpost/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

> ### ℹ️ New carrier — the detailed status list is still growing
>
> You sign in, your parcels appear as sensors and events, and a locker parcel
> shows as `at_pickup_point`. The auth flow, payload shape and happy path are
> confirmed against a real account; InPost's detailed status string has ~60
> documented values and only a handful have been seen on the wire so far. An
> unrecognised one still lands in a sensible bucket rather than breaking — see
> [How you can help](#how-you-can-help) if you spot one.

A custom Home Assistant integration that tracks your [InPost](https://inpost.pl) parcels — the Paczkomat locker network that carries much of Poland's e-commerce (and a growing share of Italy's). You sign in the way the InPost app does: a phone number and a one-time SMS code. Your parcels then appear automatically, no tracking numbers to type.

What makes InPost worth its own integration is the **locker**: a parcel waiting for you reports `at_pickup_point`, and its Paczkomat's name comes along with it — so "notify me when a parcel is ready to collect" is a one-line automation.

Part of the [ha-parcel-integrations](https://github.com/ha-parcel-integrations) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Examples](#examples)
- [Debugging](#debugging)
- [How you can help](#how-you-can-help)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Signs in the way the InPost app does — phone number plus an SMS code — and then reads your whole parcel inbox automatically. Nothing to type per parcel.
- Per-parcel sensor with the canonical status (`in_transit` / `out_for_delivery` / `at_pickup_point` / `delivered` / …), InPost's own status text, and — for a parcel waiting in a locker — the Paczkomat name.
- Summary sensors: incoming parcels and recently delivered parcels.
- Events + device triggers for no-code automations (parcel registered, status changed, delivered).
- Opt-in per-parcel status history.
- Manual refresh button and a diagnostic last-update sensor.

## Requirements

- Home Assistant 2024.7 or newer
- An **InPost account** (the InPost Mobile app), reachable by SMS on its phone number

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-inpost` as an **Integration**.
3. Install **InPost** and restart Home Assistant.

### Manual

Copy `custom_components/inpost` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → InPost**, then:

1. Enter the **phone number** registered with your InPost account (e.g. `600123456` — `+48` and spaces are fine).
2. InPost texts a **login code**. Enter it.

That is it — your parcels appear on the next refresh. If the session ever expires, Home Assistant asks you to repeat the SMS step; nothing else changes.

You can add more than one account (each is a separate phone number).

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |
| Polling | Refresh every | 30 min | How often InPost is checked. Slower is gentler on their API. |

## Removal

Standard HA removal applies: **Settings → Devices & Services → InPost → ⋮ → Delete**.

## Sensors

Entity IDs include the account's phone number, so multiple accounts stay distinct.

| Entity | Description |
|---|---|
| `sensor.inpost_<phone>_incoming_parcels` | Number of active parcels, full list under the `parcels` attribute |
| `sensor.inpost_<phone>_parcel_<number>` | One per parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.inpost_<phone>_delivered_parcels` | Recently collected parcels (see the retention option) |
| `sensor.inpost_<phone>_last_successful_update` | Diagnostic: when InPost was last polled successfully |

A collected parcel moves from its per-parcel sensor to the delivered sensor automatically.

> **Note on the deliveries calendar and "next delivery" sensor:** InPost does
> not publish a delivery time window, so these stay empty. They are kept for
> consistency with the other integrations and will light up if InPost ever
> exposes an ETA.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `registered` | The sender announced the parcel; not in the network yet |
| `in_transit` | Moving through the network, including customs |
| `out_for_delivery` | With the courier, or on its way to your locker |
| `at_pickup_point` | **Waiting for you in a Paczkomat or point** |
| `delivered` | Collected |
| `returning` | Going back to the sender |
| `problem` | InPost reports an exception (failed delivery, expired pickup, …) |
| `unknown` | A status we have not mapped yet |

InPost's own detailed status string is always available as `raw_status`. When a parcel is waiting for you, the `pickup_point` attribute holds the Paczkomat name.

## Events

The integration fires these on the event bus (also available as device triggers on the InPost device):

| Event | When |
|---|---|
| `inpost_parcel_registered` | A new parcel appears in your inbox |
| `inpost_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `inpost_parcel_delivered` | A parcel is collected |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Examples

Ready-to-paste automations live in [`examples/`](examples/) — including notifying a phone when a parcel is ready to collect.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.inpost: debug
```

## How you can help

InPost describes a parcel's state with a detailed status string — `ready_to_pickup`, `out_for_delivery`, `collected_by_customer`, and dozens more. This release maps the documented set, but the list is not guaranteed complete.

An unrecognised status still lands the parcel in a sensible bucket (via InPost's coarse status group) rather than breaking, and writes one line to your log:

```
Unrecognised InPost status — help us map it. Open an issue and paste this line: …
  status=some_new_status → reported as 'unknown'
```

[Opening that issue](https://github.com/ha-parcel-integrations/ha-inpost/issues/new?template=unrecognised_status.yml) with the logged line is all it takes. Equally useful: a status that reads *wrong* rather than unknown — say a parcel marked delivered while it is still in the locker.

## Troubleshooting

- **A parcel shows `unknown`** — its status is one we do not map yet; see [How you can help](#how-you-can-help).
- **Home Assistant asks me to sign in again** — InPost sessions expire; enter a fresh SMS code and everything resumes. This is normal, not a fault.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://github.com/ha-parcel-integrations) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://github.com/ha-parcel-integrations) for the current list of supported carriers.

## Disclaimer

This integration talks to the same private mobile API the InPost app uses. It is not affiliated with, endorsed by, or supported by InPost. Endpoints can change or be withdrawn without notice; be gentle with the polling interval.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
