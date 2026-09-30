"""InPost consumer mobile API client.

Two responsibilities, kept apart:

* :func:`async_exchange_code` is the one sign-in call the config flow makes —
  it turns the authorization code from the pasted callback URL into a token
  pair (see :mod:`.oauth` for the URL side of that sign-in);
* the :class:`InPostApiClient` holds the access/refresh token pair and fetches
  the parcel inbox, transparently refreshing the token on a 401.

Two token kinds reach the same inbox. Entries set up through the InPost Group
sign-in hold an OAuth pair, sent as ``Bearer`` and refreshed at the sign-in's
token endpoint. Entries set up before that, with an SMS code, still hold the
app's own pair, sent bare and refreshed at ``/v1/authenticate`` until their
next reauth moves them over.

Contract the rest of the integration relies on:

* :meth:`InPostApiClient.async_get_parcels` returns the account's parcels as a
  list of raw dicts;
* a refresh that fails raises :class:`InPostAuthReauthRequired`, which the
  coordinator turns into ``ConfigEntryAuthFailed`` so HA asks the user to
  sign in again — distinct from :class:`InPostApiError` (a transient outage
  that should retry);
* ``aiohttp.ClientError`` propagates untouched where the coordinator can wrap it
  into ``UpdateFailed``.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import aiohttp

from ..const import AUTH_METHOD_SMS, AUTH_METHOD_SSO
from .oauth import OAUTH_CLIENT_ID, OAUTH_REDIRECT_URI, OAUTH_TOKEN_URL

API_BASE = "https://api-inmobile-pl.easypack24.net"
# Refreshes the app's own token pair, held by entries set up with an SMS code.
AUTHENTICATE_URL = f"{API_BASE}/v1/authenticate"
PARCELS_URL = f"{API_BASE}/v3/parcels/tracked"

# The app identifies itself with its own User-Agent and an API-version header;
# both are sent on every request, authenticated or not.
USER_AGENT = "InPost-Mobile/3.27.2 (Android 14; SDK 34) okhttp/4.11.0"
API_VERSION = "1"
# InPost tags requests with the device platform; the app sends "Android".
PHONE_OS = "Android"

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=20)

# Sent on every request, authenticated or not — the app fingerprints itself.
_BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Content-Type": "application/json",
    "X-Api-Version": API_VERSION,
}


class InPostApiError(Exception):
    """Raised when an InPost API call fails for a transient / non-auth reason."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code that triggered the error."""
        super().__init__(f"InPost API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


def _retry_after(response: aiohttp.ClientResponse) -> float | None:
    """Return numeric Retry-After seconds when the server provides it."""
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None


class InPostAuthReauthRequired(InPostApiError):
    """Raised when the session cannot be recovered and the user must sign in again.

    Distinct from :class:`InPostApiError` on purpose: only this one may trigger
    Home Assistant's reauth flow. A plain outage must retry, never re-prompt.
    """


def _extract_tokens(payload: Any) -> tuple[str, str] | None:
    """Pull ``(authToken, refreshToken)`` out of an app token response, or ``None``."""
    if not isinstance(payload, dict):
        return None
    auth = payload.get("authToken")
    refresh = payload.get("refreshToken")
    if isinstance(auth, str) and auth and isinstance(refresh, str) and refresh:
        return auth, refresh
    return None


async def _async_post_oauth_token(
    session: aiohttp.ClientSession, body: dict[str, str]
) -> dict[str, Any]:
    """POST to the sign-in's token endpoint and return a response with a token.

    A 400/401 is the identity provider refusing the code or refresh token
    (``invalid_grant``) and raises :class:`InPostAuthReauthRequired`; anything
    else unusable is a transient :class:`InPostApiError`.
    """
    try:
        async with session.post(
            OAUTH_TOKEN_URL,
            data=body,
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        ) as response:
            status = response.status
            retry_after = _retry_after(response)
            try:
                payload = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                payload = None
    except aiohttp.ClientError as err:
        raise InPostApiError(f"token request transport error: {err}") from err

    if status == 200 and isinstance(payload, dict) and payload.get("access_token"):
        return payload
    if status in (400, 401):
        error = payload.get("error") if isinstance(payload, dict) else None
        _LOGGER.warning(
            "InPost sign-in rejected the token request: HTTP %s (%s)", status, error
        )
        raise InPostAuthReauthRequired(f"token request HTTP {status}", status_code=status)
    raise InPostApiError(
        f"token request HTTP {status}", status_code=status, retry_after=retry_after
    )


async def async_exchange_code(
    session: aiohttp.ClientSession, code: str, code_verifier: str
) -> tuple[str, str]:
    """Exchange the pasted authorization code for ``(access_token, refresh_token)``.

    The code and verifier are single-use; the caller discards them whether
    this succeeds or not.
    """
    payload = await _async_post_oauth_token(
        session,
        {
            "grant_type": "authorization_code",
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "code_verifier": code_verifier,
            "code": code,
        },
    )
    refresh = payload.get("refresh_token")
    if not (isinstance(refresh, str) and refresh):
        raise InPostApiError("sign-in response carried no refresh token")
    return payload["access_token"], refresh


class InPostApiClient:
    """Authenticated client for the InPost parcel inbox.

    Holds the token pair in memory and refreshes it on demand. When the tokens
    rotate, ``on_tokens_updated`` is invoked so the caller can persist the new
    pair into the config entry — otherwise a restart would fall back to a stale
    refresh token.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        auth_token: str,
        refresh_token: str,
        on_tokens_updated: Callable[[str, str], None] | None = None,
        *,
        auth_method: str = AUTH_METHOD_SMS,
    ) -> None:
        """Initialise with a session, the stored token pair and its kind."""
        self._session = session
        self._auth_token = auth_token
        self._refresh_token = refresh_token
        self._on_tokens_updated = on_tokens_updated
        self._auth_method = auth_method
        # Serialise refreshes: two concurrent 401s must not both refresh and
        # invalidate each other's new token.
        self._refresh_lock = asyncio.Lock()

    @property
    def tokens(self) -> tuple[str, str]:
        """The current ``(auth_token, refresh_token)`` pair."""
        return self._auth_token, self._refresh_token

    async def async_get_parcels(self) -> list[dict[str, Any]]:
        """Return the account's tracked parcels as raw payload dicts."""
        payload = await self._get(PARCELS_URL)
        parcels = payload.get("parcels")
        if not isinstance(parcels, list):
            raise InPostApiError("parcel list missing from response")
        return [parcel for parcel in parcels if isinstance(parcel, dict)]

    async def _get(self, url: str) -> dict[str, Any]:
        """GET ``url`` with auth, refreshing once on a 401."""
        status, payload, retry_after = await self._authed_get(url)
        if status == 401:
            # The access token expired; refresh and try exactly once more.
            await self._refresh()
            status, payload, retry_after = await self._authed_get(url)

        if status != 200:
            raise InPostApiError(
                f"GET {url} HTTP {status}",
                status_code=status,
                retry_after=retry_after,
            )
        if not isinstance(payload, dict):
            raise InPostApiError("unexpected body (not a JSON object)")
        return payload

    async def _authed_get(self, url: str) -> tuple[int, Any, float | None]:
        """Perform one authenticated GET; return ``(status, parsed_body|None)``."""
        # The app's own token is sent bare; the sign-in's token is a real Bearer.
        authorization = (
            f"Bearer {self._auth_token}"
            if self._auth_method == AUTH_METHOD_SSO
            else self._auth_token
        )
        headers = {**_BASE_HEADERS, "Authorization": authorization}
        async with self._session.get(
            url, headers=headers, timeout=_TIMEOUT
        ) as response:
            if response.status == 200:
                return response.status, await response.json(content_type=None), None
            return response.status, None, _retry_after(response)

    async def _refresh(self) -> None:
        """Refresh the access token, or raise :class:`InPostAuthReauthRequired`.

        Guarded by a lock, and the token captured before the lock is compared
        after acquiring it: if another coroutine already refreshed while we
        waited, we keep its newer token instead of spending our refresh token a
        second time.
        """
        async with self._refresh_lock:
            token_before = self._auth_token
            if self._auth_method == AUTH_METHOD_SSO:
                tokens = await self._refresh_oauth()
            else:
                tokens = await self._refresh_app_token()

            if token_before != self._auth_token:
                # Another coroutine refreshed while we waited for the lock.
                return

            self._auth_token, self._refresh_token = tokens
            if self._on_tokens_updated is not None:
                self._on_tokens_updated(self._auth_token, self._refresh_token)

    async def _refresh_oauth(self) -> tuple[str, str]:
        """Refresh a sign-in token pair at the identity provider."""
        payload = await _async_post_oauth_token(
            self._session,
            {
                "grant_type": "refresh_token",
                "client_id": OAUTH_CLIENT_ID,
                "refresh_token": self._refresh_token,
            },
        )
        # Seen live keeping the same refresh token; persist a new one if it
        # does rotate, otherwise keep the stored one.
        refresh = payload.get("refresh_token")
        if not (isinstance(refresh, str) and refresh):
            refresh = self._refresh_token
        return payload["access_token"], refresh

    async def _refresh_app_token(self) -> tuple[str, str]:
        """Refresh the app's own token pair held by an SMS-era entry."""
        try:
            async with self._session.post(
                AUTHENTICATE_URL,
                json={"refreshToken": self._refresh_token, "phoneOS": PHONE_OS},
                headers=_BASE_HEADERS,
                timeout=_TIMEOUT,
            ) as response:
                if response.status == 429:
                    raise InPostApiError(
                        "token refresh HTTP 429",
                        status_code=429,
                        retry_after=_retry_after(response),
                    )
                if response.status != 200:
                    # The coordinator collapses this into a generic
                    # "session expired" — keep the real reason visible.
                    body = await response.text()
                    _LOGGER.warning(
                        "Token refresh rejected: HTTP %s, body: %.300s",
                        response.status,
                        body,
                    )
                    raise InPostAuthReauthRequired(
                        f"token refresh HTTP {response.status}"
                    )
                payload = await response.json(content_type=None)
        except aiohttp.ClientError as err:
            # A transport failure during refresh is transient, not a dead
            # session — let the coordinator retry rather than force reauth.
            raise InPostApiError(f"token refresh transport error: {err}") from err

        tokens = _extract_tokens(payload)
        if tokens is None:
            # InPost may rotate only the access token: the authenticate
            # response can carry authToken alone (observed live:
            # ['authToken', 'pushIdStatus', 'reauthenticationRequired']).
            # The stored refresh token is still valid — keep it.
            auth = payload.get("authToken") if isinstance(payload, dict) else None
            if not (isinstance(auth, str) and auth):
                raise InPostAuthReauthRequired("token refresh carried no tokens")
            _LOGGER.debug(
                "Token refresh returned authToken only; keeping stored refreshToken"
            )
            tokens = (auth, self._refresh_token)
        return tokens
