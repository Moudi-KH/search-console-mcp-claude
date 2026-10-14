"""Thin read-only client for the Search Console searchAnalytics.query API.

Only one endpoint is ever called, with the read-only scope. Google libraries
are imported lazily so the rest of the server (and its tests) can run
without them.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Protocol
from urllib.parse import quote

log = logging.getLogger(__name__)

API_URL = "https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
TIMEOUT_SECONDS = 30


class GscError(Exception):
    """A Search Console call failed. `public_message` is safe to show the client."""

    def __init__(self, status: int, public_message: str):
        super().__init__(f"Search Console API error {status}")
        self.status = status
        self.public_message = public_message


class SearchConsoleQuery(Protocol):
    def query(self, site: str, body: dict[str, Any]) -> dict[str, Any]: ...


class SearchConsoleClient:
    def __init__(self, sa_key_b64: str | None = None):
        self._sa_key_b64 = sa_key_b64
        self._session: Any = None

    def _get_session(self) -> Any:
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            if self._sa_key_b64:
                from google.oauth2 import service_account

                info = json.loads(base64.b64decode(self._sa_key_b64))
                credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            else:
                # Keyless: the Cloud Run service account via Application Default Credentials.
                credentials, _ = google.auth.default(scopes=SCOPES)
            self._session = AuthorizedSession(credentials)
        return self._session

    def query(self, site: str, body: dict[str, Any]) -> dict[str, Any]:
        url = API_URL.format(site=quote(site, safe=""))
        try:
            response = self._get_session().post(url, json=body, timeout=TIMEOUT_SECONDS)
        except Exception as exc:  # network / auth failures: details stay in server logs
            log.error("Search Console request failed: %s", type(exc).__name__)
            raise GscError(502, "Could not reach Google Search Console.") from exc

        if response.status_code == 200:
            return response.json()

        log.warning("Search Console returned HTTP %s", response.status_code)
        if response.status_code == 400:
            # Google's validation message helps Claude correct the query and contains no secrets.
            try:
                detail = str(response.json()["error"]["message"])[:300]
            except Exception:
                detail = "invalid query"
            raise GscError(400, f"Search Console rejected the query: {detail}")
        if response.status_code in (401, 403):
            raise GscError(403, "The connector's service account has no access to this property.")
        if response.status_code == 429:
            raise GscError(429, "Search Console quota exceeded. Try again later.")
        raise GscError(502, "Search Console request failed.")
