"""Pure helpers for the InPost Group sign-in (OAuth 2.0 authorization code + PKCE).

InPost's own login page needs a captcha, and its only redirect is an
InPost-owned callback page, so Home Assistant cannot host the sign-in. The
config flow builds the authorization URL here, the user signs in in their own
browser, and pastes back the callback URL it lands on. The token requests
themselves live in :mod:`.client`, next to the rest of the token lifecycle.

No I/O in this module.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

OAUTH_AUTHORIZE_URL = "https://account.inpost-group.com/oauth2/authorize"
OAUTH_TOKEN_URL = "https://account.inpost-group.com/oauth2/token"
OAUTH_REDIRECT_URI = "https://account.inpost-group.com/callback"
OAUTH_CLIENT_ID = "inpost-mobile"
OAUTH_SCOPE = "openid"

# The market the account inbox serves. The token's own ``market`` claim, not
# this parameter, decides which backend the account lives on; a token for
# another market is refused by the inbox this integration reads.
SUPPORTED_MARKET = "PL"


def generate_pkce() -> tuple[str, str]:
    """Return a fresh ``(code_verifier, code_challenge)`` pair (S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    """Return a fresh random ``state`` value for one authorization request."""
    return secrets.token_urlsafe(24)


def generate_nonce() -> str:
    """Return a fresh random ``nonce`` value for one authorization request."""
    return secrets.token_urlsafe(24)


def build_authorization_url(
    code_challenge: str, state: str, nonce: str, language: str | None
) -> str:
    """Build the browser sign-in URL for one flow."""
    params = {
        "response_type": "code",
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "response_mode": "query",
        "lang": "pl-PL" if (language or "").lower().startswith("pl") else "en",
        "supported_markets": SUPPORTED_MARKET,
    }
    return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def is_valid_callback_url(value: str) -> bool:
    """Whether a pasted URL is HTTPS on the InPost callback host and path.

    Checked before the code is ever used: a URL that fails this was not
    produced by the real sign-in and must not be exchanged.
    """
    parsed = urlparse(value.strip())
    expected = urlparse(OAUTH_REDIRECT_URI)
    return (
        parsed.scheme == "https"
        and parsed.netloc == expected.netloc
        and parsed.path == expected.path
    )


def parse_callback_url(value: str) -> tuple[str | None, str | None]:
    """Return ``(code, state)`` from a pasted callback URL."""
    params = parse_qs(urlparse(value.strip()).query)
    return params.get("code", [None])[0], params.get("state", [None])[0]


def decode_token_claims(token: str) -> dict[str, Any] | None:
    """Best-effort, unverified read of a JWT's claim set.

    Only used to read the account's ``market`` and ``phone`` at sign-in, never
    to authorise anything. Returns ``None`` for anything that is not a JWT.
    """
    try:
        _, payload_b64, _ = token.split(".")
    except (AttributeError, ValueError):
        return None
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
