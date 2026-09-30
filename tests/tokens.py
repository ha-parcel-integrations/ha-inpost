"""Unsigned JWTs for tests that read an InPost sign-in token's claims."""
import base64
import json


def make_jwt(claims: dict) -> str:
    """Return an unsigned JWT carrying ``claims``."""

    def _part(value: dict) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{_part({'alg': 'none'})}.{_part(claims)}.sig"
