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

Everything here is written from InPost's documented mobile-API behaviour. It has
not yet been exercised against an account we control — see CLAUDE.md.
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

    def __init__(self, detail: str) -> None:
        super().__init__(f"InPost API request failed: {detail}")
        self.detail = detail


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
        status, payload = await self._authed_get(url)
        if status == 401:
            # The access token expired; refresh and try exactly once more.
            await self._refresh()
            status, payload = await self._authed_get(url)

        if status != 200:
            raise InPostApiError(f"GET {url} HTTP {status}")
        if not isinstance(payload, dict):
            raise InPostApiError("unexpected body (not a JSON object)")
        return payload

    async def _authed_get(self, url: str) -> tuple[int, Any]:
        """Perform one authenticated GET; return ``(status, parsed_body|None)``."""
        headers = {**_BASE_HEADERS, "Authorization": self._auth_token}
        async with self._session.get(
            url, headers=headers, timeout=_TIMEOUT
        ) as response:
            if response.status == 200:
                return response.status, await response.json(content_type=None)
            return response.status, None

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
                    if response.status != 200:
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
                raise InPostAuthReauthRequired("token refresh carried no tokens")

            self._auth_token, self._refresh_token = tokens
            if self._on_tokens_updated is not None:
                self._on_tokens_updated(self._auth_token, self._refresh_token)
