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
# public-tracking hubs (normalize_tracking_parcel) expose neither
# weight/dimensions/delivery-window nor a pickup point (no locker data at
# all), but do get a per-country deep link via
# TRACKING_URL_BY_COUNTRY. These are two structurally different APIs, not a
# stronger/weaker split of the same one — see CAPABILITIES_BY_VARIANT below.
CAPABILITIES_BY_VARIANT = {
    "Account": frozenset({"pickup_point", "url", "history"}),
    # An Italian account reads a different backend whose per-parcel shape is
    # not yet confirmed with a real parcel; claim only what is certain.
    "Account (IT)": frozenset({"url", "history"}),
    "Tracking": frozenset({"url", "history"}),
}

# Fields not confirmed yet — the docs site shows them as "awaiting data".
# Move a field into the declaration above once a real parcel shows it.
PENDING_CAPABILITIES_BY_VARIANT = {
    "Account (IT)": frozenset({"weight", "dimensions", "pickup_point"}),
}

# Consumer tracking deep link per country, for a tracking-hub or Italian
# account parcel's ``url`` field. Each InPost storefront runs its own tracking page — different host,
# path and query param per country, live-confirmed 2026-08-31. A country
# missing here (should not happen for anything in TRACKING_COUNTRIES) leaves
# ``url`` as ``None`` rather than guessing a template.
TRACKING_URL_BY_COUNTRY = {
    "PL": "https://inpost.pl/en/find-parcel?number={tracking_code}",
    "IT": "https://inpost.it/trova-il-tuo-pacco?number={tracking_code}",
    "PT": "https://www.inpost.pt/seguimento-do-envio/?exp={tracking_code}&language=pt&pais=PT",
    "GB": "https://inpost.co.uk/tracking/result?parcel_code={tracking_code}",
    "ES": "https://www.inpost.es/seguimiento-del-envio/?exp={tracking_code}&language=ES",
}

# Countries a keyless public-tracking hub can be set up for.
TRACKING_COUNTRIES = ("PL", "IT", "PT", "GB", "ES")
DEFAULT_TRACKING_COUNTRY = "PL"

# Tokens are stored in the config entry's data (not options) so they survive a
# restart; the client refreshes them and writes the new pair back.
CONF_PHONE = "phone"
CONF_AUTH_TOKEN = "auth_token"
CONF_REFRESH_TOKEN = "refresh_token"
# Which kind of token pair the entry holds. Entries from before the InPost
# Group sign-in have no value and hold the app's own SMS-login pair; a reauth
# moves them to the sign-in.
CONF_AUTH_METHOD = "auth_method"
AUTH_METHOD_SMS = "sms"
AUTH_METHOD_SSO = "sso"
# The market the account is registered in, which decides the parcel backend.
# Entries from before the market choice have no value and are Polish.
CONF_MARKET = "market"
ACCOUNT_MARKETS = ("PL", "IT")
DEFAULT_ACCOUNT_MARKET = "PL"
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
