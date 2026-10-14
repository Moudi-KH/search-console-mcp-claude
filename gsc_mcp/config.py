"""Runtime configuration, read from environment variables.

The server refuses to start if a required setting is missing or weak
(fail closed), so a misconfigured deployment never runs without auth.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

MIN_TOKEN_LENGTH = 32


@dataclass(frozen=True)
class Settings:
    # Shared secret clients must send as "Authorization: Bearer <token>".
    mcp_token: str
    # Search Console properties the tool may query, e.g. "sc-domain:example.com".
    allowed_sites: tuple[str, ...]
    # Host header values accepted (DNS-rebinding protection). Empty = no check.
    allowed_hosts: tuple[str, ...]
    # Authenticated requests allowed per minute, per server instance.
    rate_limit_per_minute: int
    # Hard cap on rows per query.
    max_row_limit: int
    # Optional base64 service-account key. Unset = use the Cloud Run identity (ADC).
    sa_key_b64: str | None


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not lo <= value <= hi:
        raise RuntimeError(f"{name} must be between {lo} and {hi}")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    token = env.get("MCP_TOKEN", "")
    if len(token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(f"MCP_TOKEN must be set and at least {MIN_TOKEN_LENGTH} characters long")

    sites = _csv(env.get("ALLOWED_SITES"))
    if not sites:
        raise RuntimeError("ALLOWED_SITES must list at least one Search Console property")

    return Settings(
        mcp_token=token,
        allowed_sites=sites,
        allowed_hosts=_csv(env.get("ALLOWED_HOSTS")),
        rate_limit_per_minute=_int(env, "RATE_LIMIT_PER_MINUTE", 60, 1, 10_000),
        max_row_limit=_int(env, "MAX_ROW_LIMIT", 5000, 1, 25_000),
        sa_key_b64=env.get("SEARCH_CONSOLE_KEY") or None,
    )
