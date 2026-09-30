"""Keyless client for InPost's public one-parcel tracking surface."""
from __future__ import annotations

from typing import Any

import aiohttp

from ..account.client import _TIMEOUT, InPostApiError, _retry_after

# Public, keyless tracking surface.  All currently supported delivery markets
# share this host and response shape; country remains entry routing data so a
# future national backend can diverge without migrating existing hubs.
EASY_TRACKING_URL = "https://inposteasy.com/api/tracking/{tracking_code}"


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
