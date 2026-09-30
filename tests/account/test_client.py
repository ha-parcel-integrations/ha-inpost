"""Tests for the InPost account client — sign-in, both token kinds and refresh."""
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.inpost.account.client import (
    InPostApiClient,
    InPostApiError,
    InPostAuthReauthRequired,
    async_exchange_code,
)
from custom_components.inpost.const import AUTH_METHOD_SSO

from ..payloads import ACTIVE_CODE, ready_sample, response
from ..sessions import fake_session as _session

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
    """InPost rotates only the access token; the stored refresh token stays."""
    session = _session(
        (401, None),
        (200, {"authToken": "acc-2"}),  # no new refreshToken
        (200, response(ready_sample())),
    )
    persisted = []
    client = InPostApiClient(
        session, "acc-1", "ref-1", on_tokens_updated=lambda a, r: persisted.append((a, r))
    )

    parcels = await client.async_get_parcels()

    assert len(parcels) == 1
    assert client.tokens == ("acc-2", "ref-1")
    # the partially rotated pair still reaches the persistence callback
    assert persisted == [("acc-2", "ref-1")]


async def test_refresh_without_any_token_demands_reauth():
    """A 200 refresh that carries no authToken at all is a dead session."""
    session = _session(
        (401, None),
        (200, {"pushIdStatus": "ok"}),  # no tokens at all
        (200, response()),
    )
    client = InPostApiClient(session, "acc-1", "ref-1")
    with pytest.raises(InPostAuthReauthRequired):
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


# ---------------------------------------------------------------------------
# InPost Group sign-in
# ---------------------------------------------------------------------------


async def test_exchange_code_returns_the_token_pair():
    session = _session(
        (200, {"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 7199})
    )
    assert await async_exchange_code(session, "the-code", "the-verifier") == (
        "acc-1",
        "ref-1",
    )
    body = session.post.call_args.kwargs["data"]
    assert body == {
        "grant_type": "authorization_code",
        "client_id": "inpost-mobile",
        "redirect_uri": "https://account.inpost-group.com/callback",
        "code_verifier": "the-verifier",
        "code": "the-code",
    }
    assert "client_secret" not in body


async def test_exchange_code_rejected_needs_a_new_sign_in():
    session = _session((400, {"error": "invalid_grant"}))
    with pytest.raises(InPostAuthReauthRequired):
        await async_exchange_code(session, "stale", "verifier")


async def test_exchange_code_without_refresh_token_is_an_error():
    session = _session((200, {"access_token": "acc-1"}))
    with pytest.raises(InPostApiError) as err:
        await async_exchange_code(session, "code", "verifier")
    assert not isinstance(err.value, InPostAuthReauthRequired)


async def test_exchange_code_server_error_is_transient():
    session = _session((502, None))
    with pytest.raises(InPostApiError) as err:
        await async_exchange_code(session, "code", "verifier")
    assert not isinstance(err.value, InPostAuthReauthRequired)
    assert err.value.status_code == 502


async def test_exchange_code_unreadable_body_is_transient():
    session = _session((200, None))
    session.post.side_effect = None
    resp = session.post.return_value.__aenter__.return_value = AsyncMock()
    resp.status = 200
    resp.headers = {}
    resp.json = AsyncMock(side_effect=ValueError("not json"))
    with pytest.raises(InPostApiError) as err:
        await async_exchange_code(session, "code", "verifier")
    assert not isinstance(err.value, InPostAuthReauthRequired)


async def test_exchange_code_transport_error_is_transient():
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(InPostApiError) as err:
        await async_exchange_code(session, "code", "verifier")
    assert not isinstance(err.value, InPostAuthReauthRequired)


def _sso_client(session, **kwargs) -> InPostApiClient:
    return InPostApiClient(session, "acc-1", "ref-1", auth_method=AUTH_METHOD_SSO, **kwargs)


async def test_sign_in_token_is_sent_as_bearer():
    session = _session((200, response(ready_sample())))
    await _sso_client(session).async_get_parcels()
    assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer acc-1"


async def test_sign_in_401_refreshes_at_the_identity_provider():
    session = _session(
        (401, None),
        (200, {"access_token": "acc-2", "refresh_token": "ref-2"}),
        (200, response(ready_sample())),
    )
    persisted = []
    client = _sso_client(session, on_tokens_updated=lambda a, r: persisted.append((a, r)))

    assert len(await client.async_get_parcels()) == 1
    assert session.post.call_args.args[0] == "https://account.inpost-group.com/oauth2/token"
    assert session.post.call_args.kwargs["data"] == {
        "grant_type": "refresh_token",
        "client_id": "inpost-mobile",
        "refresh_token": "ref-1",
    }
    assert persisted == [("acc-2", "ref-2")]
    assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer acc-2"


async def test_sign_in_refresh_keeps_the_refresh_token_when_not_rotated():
    session = _session(
        (401, None),
        (200, {"access_token": "acc-2", "expires_in": 7199}),
        (200, response()),
    )
    client = _sso_client(session)
    await client.async_get_parcels()
    assert client.tokens == ("acc-2", "ref-1")


@pytest.mark.parametrize("status", [400, 401])
async def test_sign_in_refresh_rejected_needs_a_new_sign_in(status):
    session = _session((401, None), (status, {"error": "invalid_grant"}))
    with pytest.raises(InPostAuthReauthRequired):
        await _sso_client(session).async_get_parcels()


async def test_sign_in_refresh_rate_limited_is_transient_with_retry_after():
    session = _session((401, None), (429, None))
    session.post.side_effect = None
    resp = AsyncMock()
    resp.status = 429
    resp.headers = {"Retry-After": "90"}
    resp.json = AsyncMock(return_value=None)
    session.post.return_value.__aenter__ = AsyncMock(return_value=resp)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    with pytest.raises(InPostApiError) as err:
        await _sso_client(session).async_get_parcels()
    assert not isinstance(err.value, InPostAuthReauthRequired)
    assert err.value.retry_after == 90
