"""ASGI middleware: bearer-token auth and rate limiting.

Runs in front of the MCP app, so unauthenticated requests are rejected
before any request body is read or parsed.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from collections import deque
from collections.abc import Callable

from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)


class BearerAuthRateLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        token: str,
        requests_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.app = app
        self._expected = f"Bearer {token}".encode()
        self._limit = requests_per_minute
        self._clock = clock
        self._hits: deque[float] = deque()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan events pass through
            await self.app(scope, receive, send)
            return

        provided = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                provided = value
                break

        # Constant-time comparison (CWE-208).
        if not hmac.compare_digest(provided, self._expected):
            log.warning("Rejected request: missing or invalid token")
            await _json(send, 401, {"error": "unauthorized"}, {b"www-authenticate": b'Bearer realm="gsc-mcp"'})
            return

        now = self._clock()
        while self._hits and now - self._hits[0] >= 60:
            self._hits.popleft()
        if len(self._hits) >= self._limit:
            log.warning("Rate limit exceeded")
            await _json(send, 429, {"error": "rate limit exceeded"}, {b"retry-after": b"60"})
            return
        self._hits.append(now)

        await self.app(scope, receive, send)


async def _json(send: Send, status: int, body: dict, extra_headers: dict[bytes, bytes]) -> None:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    headers.extend(extra_headers.items())
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})
