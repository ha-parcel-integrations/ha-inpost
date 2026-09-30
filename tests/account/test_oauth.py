"""Tests for the pure InPost Group sign-in helpers."""
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from custom_components.inpost.account.oauth import (
    OAUTH_AUTHORIZE_URL,
    build_authorization_url,
    decode_token_claims,
    generate_pkce,
    is_valid_callback_url,
    parse_callback_url,
)

from ..tokens import make_jwt


def test_pkce_challenge_is_the_s256_of_the_verifier():
    verifier, challenge = generate_pkce()
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert generate_pkce()[0] != verifier


def test_authorization_url_carries_the_app_client_and_polish_market():
    url = build_authorization_url("chal", "st", "no", "en")
    assert url.startswith(f"{OAUTH_AUTHORIZE_URL}?")
    params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert params == {
        "response_type": "code",
        "client_id": "inpost-mobile",
        "redirect_uri": "https://account.inpost-group.com/callback",
        "scope": "openid",
        "code_challenge": "chal",
        "code_challenge_method": "S256",
        "state": "st",
        "nonce": "no",
        "response_mode": "query",
        "lang": "en",
        "supported_markets": "PL",
    }


@pytest.mark.parametrize(
    ("language", "expected"), [("pl", "pl-PL"), ("pl-PL", "pl-PL"), ("nl", "en"), (None, "en")]
)
def test_authorization_url_language(language, expected):
    url = build_authorization_url("c", "s", "n", language)
    assert parse_qs(urlparse(url).query)["lang"] == [expected]


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("https://account.inpost-group.com/callback?code=abc&state=xyz", True),
        ("  https://account.inpost-group.com/callback?code=abc&state=xyz\n", True),
        ("http://account.inpost-group.com/callback?code=abc", False),
        ("https://evil.example/callback?code=abc", False),
        ("https://account.inpost-group.com/other?code=abc", False),
        ("not a url", False),
        ("", False),
    ],
)
def test_callback_url_validation(value, valid):
    assert is_valid_callback_url(value) is valid


def test_parse_callback_url_returns_code_and_state():
    assert parse_callback_url(
        "https://account.inpost-group.com/callback?code=abc&state=xyz"
    ) == ("abc", "xyz")
    assert parse_callback_url("https://account.inpost-group.com/callback") == (None, None)


def test_decode_token_claims_reads_the_payload():
    token = make_jwt({"market": "PL", "phone": "600123456"})
    assert decode_token_claims(token) == {"market": "PL", "phone": "600123456"}


@pytest.mark.parametrize(
    "token", ["", "not-a-jwt", "a.b", "a.!!!.c", "a.bm90LWpzb24.c", "a.WzFd.c", None]
)
def test_decode_token_claims_rejects_malformed_tokens(token):
    assert decode_token_claims(token) is None
