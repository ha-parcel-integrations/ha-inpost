# InPost — still to do

The integration is complete and its tests pass, but **it has never run against
an InPost account we control.** The auth mechanics are verified against a
working community integration and the payload against InPost's documented mobile
API — but a real phone number and a real parcel are what turn "well-evidenced"
into "confirmed". That is the gap this file tracks.

## Before a 1.0.0 release

- [ ] **Log in with a real Polish (or Italian) InPost account.** This is the
      one thing nothing else can substitute — it is the only way to confirm the
      SMS flow, the bare-token header, `/v3/parcels/tracked`, and the token
      refresh all behave as coded.
- [ ] **Capture one fully populated `/v3/parcels/tracked` response** (redacted)
      and drop it into `tests/payloads.py`, replacing the modelled samples.
- [ ] **Confirm the parcel field names** — `shipmentNumber`, `status`,
      `statusGroup`, `sender.name`, `pickUpPoint.name`, `pickUpDate`,
      `operations.collect`, `events[].date/eventTitle`. All are read defensively,
      but a real response removes the guesswork.
- [ ] **Extend / correct the status map.** `STATUS_MAP` in `const.py` covers the
      documented values; real parcels will surface any that are missing or
      mis-bucketed. The pickup family (`ready_to_pickup`, `stack_in_box_machine`,
      `stack_*_pickup_time_expired`) is the highest-stakes group — a wrong call
      there fires the delivered event while a parcel sits in a locker.

## Nice-to-have, once the basics are confirmed

- [ ] **QR-code pickup entity.** `qrCode` / `openCode` are already in `raw`
      (and redacted in diagnostics). An `image` platform rendering the QR — the
      one you show at the Paczkomat — would be a standout feature. PostNL's
      `image.py` (label image) is the pattern to follow. Deliberately left out
      of 0.9.0.
- [ ] **Locker directory.** `/v1/points` lists every Paczkomat, keyless. Could
      enrich `pickup_point` with address / opening hours / coordinates.
- [ ] **Italy.** The `-it` host exists; the account flow is presumably identical.
      Confirm with an Italian account before claiming support.

## Suite integration

- [ ] Add `inpost` to the aggregator's `KNOWN_CARRIERS` and
      `CARRIER_EVENT_PREFIXES`. (Already reserved-safe: an unknown carrier is
      silently skipped.)

## Already verified

- Auth flow, bare-token header and `/v3/parcels/tracked` — from a working
  community integration.
- Field names and the ~60-value status vocabulary — from InPost's documented
  mobile API, cross-checked against that integration.

Delete this file once it is empty.
