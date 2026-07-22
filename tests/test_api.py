"""Tests for the InPost API client — SMS login helpers and token refresh."""
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.inpost.api import (
    InPostApiClient,
    InPostApiError,
    InPostAuthReauthRequired,
    async_confirm_sms_code,
    async_send_sms_code,
)

from .payloads import ACTIVE_CODE, response, ready_sample


def _session(*responses) -> MagicMock:
    """A session whose get/post return the queued ``(status, body)`` responses.

    Each queued item is used for one call, in order; a single item is reused for
    every call.
    """
    queue = list(responses)

    def _make(item):
        status, body = item
        resp = AsyncMock()
        resp.status = status
        resp.json = AsyncMock(return_value=body)
        resp.headers = {}
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    def _next(*args, **kwargs):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return _make(item)

    session = MagicMock()
    session.get = MagicMock(side_effect=_next)
    session.post = MagicMock(side_effect=_next)
    return session


# ---------------------------------------------------------------------------
# SMS login helpers
# ---------------------------------------------------------------------------


async def test_send_sms_accepts_any_2xx():
    session = _session((200, None))
    await async_send_sms_code(session, "600123456")
    assert session.post.call_args.kwargs["json"] == {"phoneNumber": "600123456"}


async def test_send_sms_raises_on_error():
    with pytest.raises(InPostApiError):
        await async_send_sms_code(_session((500, None)), "600123456")


async def test_confirm_sms_returns_token_pair():
    session = _session(
        (200, {"authToken": "acc-1", "refreshToken": "ref-1"})
    )
    tokens = await async_confirm_sms_code(session, "600123456", "1234")
    assert tokens == ("acc-1", "ref-1")
    body = session.post.call_args.kwargs["json"]
    assert body == {"phoneNumber": "600123456", "smsCode": "1234", "phoneOS": "Android"}


async def test_confirm_sms_rejected_code_raises():
    with pytest.raises(InPostApiError):
        await async_confirm_sms_code(_session((400, None)), "600123456", "0000")


async def test_confirm_sms_without_tokens_raises():
    with pytest.raises(InPostApiError):
        await async_confirm_sms_code(_session((200, {"authToken": ""})), "6", "1")


# ---------------------------------------------------------------------------
# authenticated client
# ---------------------------------------------------------------------------


async def test_get_parcels_returns_the_list():
    session = _session((200, response(ready_sample())))
    client = InPostApiClient(session, "acc", "ref")
    parcels = await client.async_get_parcels()
    assert parcels[0]["shipmentNumber"] == ACTIVE_CODE
    # the bare token is sent as Authorization, not "Bearer <token>"
    assert session.get.call_args.kwargs["headers"]["Authorization"] == "acc"


async def test_get_parcels_skips_non_dict_entries():
    session = _session((200, {"parcels": [ready_sample(), "junk", 5]}))
    client = InPostApiClient(session, "acc", "ref")
    assert len(await client.async_get_parcels()) == 1


async def test_get_parcels_raises_without_a_list():
    session = _session((200, {"parcels": "nope"}))
    with pytest.raises(InPostApiError):
        await InPostApiClient(session, "acc", "ref").async_get_parcels()


async def test_401_triggers_refresh_then_retry():
    """The token expired mid-poll: refresh, then the retry succeeds."""
    session = _session(
        (401, None),  # GET parcels -> expired
        (200, {"authToken": "acc-2", "refreshToken": "ref-2"}),  # refresh
        (200, response(ready_sample())),  # GET parcels retry
    )
    persisted = []
    client = InPostApiClient(
        session, "acc-1", "ref-1", on_tokens_updated=lambda a, r: persisted.append((a, r))
    )

    parcels = await client.async_get_parcels()

    assert len(parcels) == 1
    assert client.tokens == ("acc-2", "ref-2")
    # the rotated pair was handed to the persistence callback
    assert persisted == [("acc-2", "ref-2")]


async def test_refresh_keeps_old_refresh_token_when_response_omits_it():
    session = _session(
        (401, None),
        (200, {"authToken": "acc-2"}),  # no new refreshToken
        (200, response()),
    )
    client = InPostApiClient(session, "acc-1", "ref-1")
    with pytest.raises(InPostAuthReauthRequired):
        # authToken present but refreshToken missing -> not a valid pair
        await client.async_get_parcels()


async def test_failed_refresh_demands_reauth():
    """A 401 whose refresh also fails is a dead session, not a retry."""
    session = _session((401, None), (401, None))
    client = InPostApiClient(session, "acc", "ref")
    with pytest.raises(InPostAuthReauthRequired):
        await client.async_get_parcels()


async def test_refresh_transport_error_is_transient_not_reauth():
    """A network blip during refresh should retry, never force reauth."""
    session = MagicMock()
    call = {"n": 0}

    def _get(*a, **k):
        resp = AsyncMock()
        resp.status = 401
        resp.headers = {}
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session.get = MagicMock(side_effect=_get)
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    client = InPostApiClient(session, "acc", "ref")
    with pytest.raises(InPostApiError) as err:
        await client.async_get_parcels()
    assert not isinstance(err.value, InPostAuthReauthRequired)


async def test_non_401_error_status_raises_api_error():
    session = _session((503, None))
    with pytest.raises(InPostApiError):
        await InPostApiClient(session, "acc", "ref").async_get_parcels()
