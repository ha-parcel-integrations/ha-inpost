"""A fake aiohttp session shared by the client tests."""
from unittest.mock import AsyncMock, MagicMock


def fake_session(*responses) -> MagicMock:
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
