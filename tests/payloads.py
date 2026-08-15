"""Sample InPost API payloads shared by the test modules.

Modelled on InPost's documented ``TrackedParcelNetwork`` shape and the app's
auth responses, cross-checked 2026-08-15 against a real account's response
(``eventLog`` shape, the ``avizo`` status) — see ``carrier-research/inpost.md``.
"""
from __future__ import annotations

ACTIVE_CODE = "630400123456789012345678"
DELIVERED_CODE = "630400987654321098765432"


def event(name: str, date: str) -> dict:
    """One entry of InPost's ``eventLog[]`` timeline.

    Shape confirmed against a real account 2026-08-15: ``{type, name, date}``,
    no separate human-readable title. ``name`` shares the same vocabulary as
    the parcel's own ``status`` field.
    """
    return {"type": "PARCEL_STATUS", "name": name, "date": date}


def ready_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel waiting for pickup in a Paczkomat — the headline InPost case."""
    return {
        "shipmentNumber": code,
        "shipmentType": "parcel",
        "status": "ready_to_pickup",
        "statusGroup": "READY",
        "sender": {"name": "Allegro"},
        "receiver": {"name": "Jan Kowalski"},
        "openCode": "123456",
        "qrCode": "P|123456|abcdef",
        "expiryDate": "2026-05-03T21:00:00+02:00",
        "storedDate": "2026-04-29T14:12:00+02:00",
        "parcelSize": "A",
        "pickUpPoint": {
            "name": "KRA010",
            "locationDescription": "Przy sklepie Żabka",
            "addressDetails": {"city": "Kraków", "street": "Floriańska", "postCode": "31-019"},
        },
        "operations": {"collect": True},
        "eventLog": [
            event("ADOPTED_AT_SORTING_CENTER", "2026-04-28T09:00:00+02:00"),
            event("OUT_FOR_DELIVERY", "2026-04-29T06:00:00+02:00"),
            event("READY_TO_PICKUP", "2026-04-29T14:12:00+02:00"),
        ],
    }


def in_transit_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel still moving through the network — no locker yet."""
    sample = ready_sample(code)
    sample.update(
        {
            "status": "adopted_at_sorting_center",
            "statusGroup": "IN_DELIVERY",
            "openCode": None,
            "qrCode": None,
            "expiryDate": None,
            "storedDate": None,
            "pickUpPoint": None,
            "operations": {"collect": False},
            "eventLog": sample["eventLog"][:1],
        }
    )
    return sample


# Backwards-friendly alias: the suite's shared test helpers speak of an "active"
# sample. For InPost the interesting active state is "ready in the locker".
active_sample = ready_sample


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A parcel already collected from the locker."""
    sample = ready_sample(code)
    sample.update(
        {
            "status": "collected_by_customer",
            "statusGroup": "DELIVERED",
            "pickUpDate": "2026-04-30T18:22:00+02:00",
            "operations": {"collect": False},
        }
    )
    sample["eventLog"] = sample["eventLog"] + [
        event("DELIVERED", "2026-04-30T18:22:00+02:00")
    ]
    return sample


def response(*parcels: dict) -> dict:
    """Wrap parcels in the ``/v3/parcels/tracked`` envelope."""
    return {"parcels": list(parcels)}
