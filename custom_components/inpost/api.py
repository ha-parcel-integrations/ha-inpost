"""InPost consumer mobile API client.

Two responsibilities, kept apart:

* the **login helpers** (:func:`async_send_sms_code`, :func:`async_confirm_sms_code`)
  are module-level functions the config flow uses — no tokens yet, just a phone
  number and the SMS code the user types back;
* the :class:`InPostApiClient` holds the access/refresh token pair and fetches
  the parcel inbox, transparently refreshing the token on a 401.

Contract the rest of the integration relies on:

* :meth:`InPostApiClient.async_get_parcels` returns the account's parcels as a
  list of raw dicts;
* a refresh that fails raises :class:`InPostAuthReauthRequired`, which the
  coordinator turns into ``ConfigEntryAuthFailed`` so HA re-prompts for SMS —
  distinct from :class:`InPostApiError` (a transient outage that should retry);
* ``aiohttp.ClientError`` propagates untouched where the coordinator can wrap it
  into ``UpdateFailed``.

Auth, the token refresh lifecycle and the parcel-list endpoint are confirmed
against real accounts — see CLAUDE.md.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import aiohttp

from .const import (
    API_VERSION,
    AUTHENTICATE_URL,
    CONFIRM_SMS_URL,
    EASY_TRACKING_URL,
    PARCELS_URL,
    PHONE_OS,
    SEND_SMS_URL,
    USER_AGENT,
)

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
    """Raised when the session cannot be recovered and SMS login must repeat.

    Distinct from :class:`InPostApiError` on purpose: only this one may trigger
    Home Assistant's reauth flow. A plain outage must retry, never re-prompt.
    """


def _extract_tokens(payload: Any) -> tuple[str, str] | None:
    """Pull ``(authToken, refreshToken)`` out of a token response, or ``None``."""
    if not isinstance(payload, dict):
        return None
    auth = payload.get("authToken")
    refresh = payload.get("refreshToken")
    if isinstance(auth, str) and auth and isinstance(refresh, str) and refresh:
        return auth, refresh
    return None


async def async_send_sms_code(session: aiohttp.ClientSession, phone: str) -> None:
    """Ask InPost to text an SMS login code to ``phone``.

    ``phone`` is the bare national number (digits only). Any 2xx means the code
    was dispatched; anything else raises :class:`InPostApiError`.
    """
    async with session.post(
        SEND_SMS_URL,
        json={"phoneNumber": phone},
        headers=_BASE_HEADERS,
        timeout=_TIMEOUT,
    ) as response:
        if response.status // 100 != 2:
            raise InPostApiError(f"sendSMSCode HTTP {response.status}")


async def async_confirm_sms_code(
    session: aiohttp.ClientSession, phone: str, code: str
) -> tuple[str, str]:
    """Exchange the SMS code for an ``(auth_token, refresh_token)`` pair.

    A rejected code comes back non-2xx and raises :class:`InPostApiError`; the
    config flow surfaces that as ``invalid_auth``.
    """
    async with session.post(
        CONFIRM_SMS_URL,
        json={"phoneNumber": phone, "smsCode": code, "phoneOS": PHONE_OS},
        headers=_BASE_HEADERS,
        timeout=_TIMEOUT,
    ) as response:
        if response.status // 100 != 2:
            raise InPostApiError(f"confirmSMSCode HTTP {response.status}")
        payload = await response.json(content_type=None)

    tokens = _extract_tokens(payload)
    if tokens is None:
        raise InPostApiError("confirmSMSCode response carried no tokens")
    return tokens


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
    ) -> None:
        """Initialise with a session and the stored token pair."""
        self._session = session
        self._auth_token = auth_token
        self._refresh_token = refresh_token
        self._on_tokens_updated = on_tokens_updated
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
        headers = {**_BASE_HEADERS, "Authorization": self._auth_token}
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

            if token_before != self._auth_token:
                # Another coroutine refreshed while we waited for the lock.
                return

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

            self._auth_token, self._refresh_token = tokens
            if self._on_tokens_updated is not None:
                self._on_tokens_updated(self._auth_token, self._refresh_token)


class InPostTrackingApiClient:
    """Keyless client for InPost's public one-parcel tracking surface.

    It deliberately has no token lifecycle and never raises
    :class:`InPostAuthReauthRequired`: a failure is an ordinary, retryable
    request failure for the coordinator.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise with Home Assistant's shared HTTP session."""
        self._session = session

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any]:
        """Fetch one raw public tracking response."""
        url = EASY_TRACKING_URL.format(tracking_code=tracking_code)
        async with self._session.get(
            url,
            params={"language": "en"},
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        ) as response:
            if response.status != 200:
                raise InPostApiError(
                    f"GET {url} HTTP {response.status}",
                    status_code=response.status,
                    retry_after=_retry_after(response),
                )
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise InPostApiError("unexpected tracking body (not a JSON object)")
        # The public endpoint can return a JSON body with a semantic status
        # 500 while the HTTP response itself is 200 (observed on PT). It is an
        # upstream outage, not a parcel state to expose as ``unknown``.
        if payload.get("status") == 500 and "trackingNumber" not in payload:
            raise InPostApiError("tracking response status 500", status_code=500)
        return payload
