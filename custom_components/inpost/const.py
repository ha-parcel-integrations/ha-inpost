"""Constants for the InPost parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "inpost"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# InPost's consumer mobile API — the one the Android app talks to. This is the
# *account inbox* surface: log in once with a phone number and an SMS code, and
# the account then lists every inbound parcel automatically. There is a second,
# keyless per-tracking-number endpoint (``api-shipx-*``), but it returns one
# parcel at a time and none of the locker data; the app API is the richer one.
#
# Auth is a three-step token dance, NOT a username/password login:
#   1. POST /v1/sendSMSCode      {"phoneNumber": "<digits>"}          -> 200 = SMS sent
#   2. POST /v1/confirmSMSCode   {"phoneNumber", "smsCode", "phoneOS"} -> {authToken, refreshToken}
#   3. POST /v1/authenticate     {"refreshToken", "phoneOS"}          -> refreshed {authToken, refreshToken?}
# The access token is sent as a **bare** ``Authorization: <authToken>`` header
# (not ``Bearer <token>``), and a 401 means "refresh, then retry once". When the
# refresh itself fails, the whole session is dead and HA must re-prompt for SMS.
#
# All shapes here are verified against the InPost Android app's traffic and a
# working community integration — but not yet against an account we control. See
# CLAUDE.md for the confidence caveat.
API_BASE = "https://api-inmobile-pl.easypack24.net"
SEND_SMS_URL = f"{API_BASE}/v1/sendSMSCode"
CONFIRM_SMS_URL = f"{API_BASE}/v1/confirmSMSCode"
AUTHENTICATE_URL = f"{API_BASE}/v1/authenticate"
PARCELS_URL = f"{API_BASE}/v3/parcels/tracked"

# The app identifies itself with its own User-Agent and an API-version header;
# both are sent on every request, authenticated or not.
USER_AGENT = "InPost-Mobile/3.27.2 (Android 14; SDK 34) okhttp/4.11.0"
API_VERSION = "1"
# InPost tags requests with the device platform; the app sends "Android".
PHONE_OS = "Android"

# The consumer tracking deep link, for the parcel's ``url`` field.
TRACKING_URL = "https://inpost.pl/sledzenie-przesylek?number={tracking_code}"

# Tokens are stored in the config entry's data (not options) so they survive a
# restart; the client refreshes them and writes the new pair back.
CONF_PHONE = "phone"
CONF_AUTH_TOKEN = "auth_token"
CONF_REFRESH_TOKEN = "refresh_token"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Refresh interval (minutes) controls how often the coordinator polls the
# carrier. Default 30 min keeps the load on a consumer endpoint gentle; the
# minimum is 15 min for the same reason.
#
# Deliberate divergence from the HA Core rule that polling intervals are not
# user-configurable: that rule targets core integrations, and in a HACS parcel
# tracker a tunable cadence is a wanted feature. Generate with
# ``--interval fixed`` instead when the carrier throttles or soft-bans unusual
# traffic.
CONF_REFRESH_INTERVAL = "refresh_interval"
REFRESH_INTERVAL_OPTIONS = (15, 30, 60, 120, 240)
DEFAULT_REFRESH_INTERVAL = 30

# Per-parcel status history is opt-in and off by default, identical across the
# suite. Keep it off by default: it is a large attribute, and on carriers that
# need a second call per parcel the cost is real.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20

# InPost reports status at two granularities on every parcel:
#   * ``status``      — a detailed, free-form string (~60 values), mapped below.
#   * ``statusGroup`` — a coarse UPPERCASE bucket used as a fallback.
# The two-tier design is deliberate: even a detailed status we have not mapped
# still lands in a sensible canonical bucket via its group, rather than
# ``unknown``. Only a status whose *group* is also unrecognised warns.
#
# Locker vocabulary is the interesting part — InPost has real "ready in the
# locker" and "pickup deadline expired" states no doorstep carrier has. Mapped
# from InPost's documented status list; verify against a live account.

# Detailed ``status`` string -> ParcelStatus. Lower-case keys; the wire values
# are lower-case (e.g. ``ready_to_pickup``).
STATUS_MAP: dict[str, str] = {
    # Announced by the sender, not yet in the network.
    "created": ParcelStatus.REGISTERED,
    "confirmed": ParcelStatus.REGISTERED,
    "dispatched_by_sender": ParcelStatus.REGISTERED,
    "dispatched_by_sender_to_pok": ParcelStatus.REGISTERED,
    # Moving through the network.
    "adopted_at_source_branch": ParcelStatus.IN_TRANSIT,
    "sent_from_source_branch": ParcelStatus.IN_TRANSIT,
    "adopted_at_sorting_center": ParcelStatus.IN_TRANSIT,
    "sent_from_sorting_center": ParcelStatus.IN_TRANSIT,
    "adopted_at_target_branch": ParcelStatus.IN_TRANSIT,
    "redirect_to_box": ParcelStatus.IN_TRANSIT,
    "permanently_redirected_to_box_machine": ParcelStatus.IN_TRANSIT,
    "permanently_redirected_to_customer_service_point": ParcelStatus.IN_TRANSIT,
    "readdressed": ParcelStatus.IN_TRANSIT,
    # Final leg to the door or the locker.
    "out_for_delivery": ParcelStatus.OUT_FOR_DELIVERY,
    "out_for_delivery_to_address": ParcelStatus.OUT_FOR_DELIVERY,
    # Waiting for the recipient in a locker or point.
    "ready_to_pickup": ParcelStatus.AT_PICKUP_POINT,
    "ready_for_collection": ParcelStatus.AT_PICKUP_POINT,
    "ready_to_pickup_from_branch": ParcelStatus.AT_PICKUP_POINT,
    "ready_to_pickup_from_pok": ParcelStatus.AT_PICKUP_POINT,
    "ready_to_pickup_from_pok_registered": ParcelStatus.AT_PICKUP_POINT,
    "stack_in_box_machine": ParcelStatus.AT_PICKUP_POINT,
    "stack_in_customer_service_point": ParcelStatus.AT_PICKUP_POINT,
    "pickup_reminder_sent": ParcelStatus.AT_PICKUP_POINT,
    "pickup_reminder_sent_address": ParcelStatus.AT_PICKUP_POINT,
    # Collected / delivered — terminal "arrived" states. ``claimed`` is the
    # post-pickup state of a locker parcel, so it sorts with delivered, never
    # mid-transit.
    "delivered": ParcelStatus.DELIVERED,
    "collected_by_customer": ParcelStatus.DELIVERED,
    "collected_from_sender": ParcelStatus.DELIVERED,
    "taken_by_courier": ParcelStatus.DELIVERED,
    "taken_by_courier_from_pok": ParcelStatus.DELIVERED,
    "claimed": ParcelStatus.DELIVERED,
    # Going back to the sender.
    "returned_to_sender": ParcelStatus.RETURNING,
    "return_pickup_confirmation_to_sender": ParcelStatus.RETURNING,
    # Something went wrong.
    "delay_in_delivery": ParcelStatus.PROBLEM,
    "delivery_attempt_failed": ParcelStatus.PROBLEM,
    "rejected_by_receiver": ParcelStatus.PROBLEM,
    "not_collected": ParcelStatus.PROBLEM,
    "missing": ParcelStatus.PROBLEM,
    "oversized": ParcelStatus.PROBLEM,
    "canceled": ParcelStatus.PROBLEM,
    "cancelled": ParcelStatus.PROBLEM,
    "pickup_time_expired": ParcelStatus.PROBLEM,
    "stack_parcel_pickup_time_expired": ParcelStatus.PROBLEM,
    "stack_parcel_in_box_machine_pickup_time_expired": ParcelStatus.PROBLEM,
    "avizo_rejected": ParcelStatus.PROBLEM,
    "undelivered": ParcelStatus.PROBLEM,
    "undelivered_cod_cash_receiver": ParcelStatus.PROBLEM,
    "undelivered_incomplete_address": ParcelStatus.PROBLEM,
    "undelivered_lack_of_access_letterbox": ParcelStatus.PROBLEM,
    "undelivered_no_mailbox": ParcelStatus.PROBLEM,
    "undelivered_not_live_address": ParcelStatus.PROBLEM,
    "undelivered_unknown_receiver": ParcelStatus.PROBLEM,
    "undelivered_wrong_address": ParcelStatus.PROBLEM,
}

# Coarse ``statusGroup`` -> ParcelStatus. Matched case-insensitively (the wire
# values are UPPERCASE). ``OTHER`` is intentionally absent so it falls through
# to ``unknown`` + a one-shot warning rather than being force-bucketed.
STATUS_GROUP_MAP: dict[str, str] = {
    "created": ParcelStatus.REGISTERED,
    "in_delivery": ParcelStatus.IN_TRANSIT,
    "ready": ParcelStatus.AT_PICKUP_POINT,
    "delivered": ParcelStatus.DELIVERED,
    "claimed": ParcelStatus.DELIVERED,
}
