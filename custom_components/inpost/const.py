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

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping this carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Which optional contract fields each of InPost's two backends actually
# populates — feeds the comparison table on the docs site. Keep in lockstep
# with account/parcels.py and tracking/parcels.py: the account inbox
# (normalize_parcel) exposes a pickup point and a deep link; the keyless
# public-tracking hubs (normalize_tracking_parcel) expose neither weight/dimensions/delivery-window nor a pickup point (no
# locker data at all), but do get a per-country deep link via
# TRACKING_URL_BY_COUNTRY. These are two structurally different APIs, not a
# stronger/weaker split of the same one — see CAPABILITIES_BY_VARIANT below.
CAPABILITIES_BY_VARIANT = {
    "Account": frozenset({"pickup_point", "url", "history"}),
    "Tracking": frozenset({"url", "history"}),
}

# Countries a keyless public-tracking hub can be set up for.
TRACKING_COUNTRIES = ("PL", "IT", "PT", "GB", "ES")
DEFAULT_TRACKING_COUNTRY = "PL"

# Tokens are stored in the config entry's data (not options) so they survive a
# restart; the client refreshes them and writes the new pair back.
CONF_PHONE = "phone"
CONF_AUTH_TOKEN = "auth_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_COUNTRY = "country"
CONF_PARCELS = "parcels"
CONF_TRACKING_CODE = "tracking_code"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Dynamic, status-driven polling — unconditional, with no user-facing
# interval.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1
STAGGER_MINUTES = 7

# Per-parcel status history is opt-in and off by default, identical across the
# suite. Keep it off by default: it is a large attribute, and on carriers that
# need a second call per parcel the cost is real.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
