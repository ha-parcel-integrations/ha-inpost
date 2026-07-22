# Examples

Ready-to-paste Home Assistant snippets for the InPost integration.

| Folder | Contents |
|---|---|
| [`automations/`](automations/) | YAML automations — copy them into your `automations.yaml` or paste them into the Automation editor in **raw editor** mode. |

Parcels come from your InPost account, so there is nothing to register
by hand: whatever the account knows about shows up automatically.

All examples assume a single InPost account. Adjust entity IDs to match
yours; with more than one account configured, every entity ID carries the
account name.

## Events used in the examples

The coordinator fires these on the HA event bus:

| Event | When | Payload |
|---|---|---|
| `inpost_parcel_registered` | A new parcel appears in the active list | The full normalised parcel dict |
| `inpost_parcel_status_changed` | A parcel's canonical status changes | Same, plus `old_status` / `new_status` |
| `inpost_parcel_delivered` | A parcel reaches the delivered status | Same (fires *instead of* `status_changed` on that final hop) |

Every payload also carries the account's `device_id`, which is what device
triggers filter on. Events are suppressed on the first refresh after start-up.
