"""End-to-end protocol tests: real HTTP server + the official MCP client SDK.

Starts uvicorn with tests.fake_app (fake Search Console), then talks to it
exactly the way an MCP client such as claude.ai does.
Run: python -m unittest discover -s tests -t . -v
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

SECRET_IN_ERROR = "AIzaSy-THIS-SECRET-MUST-NEVER-LEAK"  # must match tests/fake_app.py

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "p" * 48
SITE = "sc-domain:urbansportsclub.com"
TODAY = datetime.now(timezone.utc).date()
START = (TODAY - timedelta(days=30)).isoformat()
END = (TODAY - timedelta(days=3)).isoformat()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, **env):
        self.port = _free_port()
        self.call_log = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl").name
        self.env = {
            **os.environ,
            "MCP_TOKEN": TOKEN,
            "ALLOWED_SITES": SITE,
            "ALLOWED_HOSTS": f"127.0.0.1:{self.port}",
            "FAKE_CALL_LOG": self.call_log,
            **env,
        }
        self.url = f"http://127.0.0.1:{self.port}/mcp"

    def __enter__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "tests.fake_app:app", "--port", str(self.port), "--log-level", "info"],
            cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.1)
        self.proc.kill()
        raise RuntimeError(self.proc.stdout.read().decode())

    def __exit__(self, *exc):
        self.proc.terminate()
        self.logs = self.proc.communicate(timeout=10)[0].decode()

    def calls(self):
        with open(self.call_log) as fh:
            return [json.loads(line) for line in fh if line.strip()]


async def _session_run(url, fn, token=TOKEN):
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(headers=headers, timeout=30) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)


def run(url, fn, token=TOKEN):
    return asyncio.run(_session_run(url, fn, token))


def text(result):
    return "".join(getattr(c, "text", "") for c in result.content)


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__(None, None, None)
        cls.logs = cls.server.logs

    def call(self, name, args):
        return run(self.server.url, lambda s: s.call_tool(name, args))

    # --- auth (raw HTTP, no SDK) ---
    def test_no_token_401(self):
        r = httpx2.post(self.server.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status_code, 401)

    def test_wrong_token_401(self):
        r = httpx2.post(self.server.url, json={}, headers={"Authorization": "Bearer " + "q" * 48})
        self.assertEqual(r.status_code, 401)

    def test_unauthenticated_garbage_body_gets_no_parser_details(self):
        r = httpx2.post(self.server.url, content=b"{not json", headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {"error": "unauthorized"})

    def test_oversized_body_rejected(self):
        r = httpx2.post(
            self.server.url,
            content=b"x" * (200 * 1024),
            headers={"Authorization": f"Bearer {TOKEN}", "content-type": "application/json",
                     "accept": "application/json, text/event-stream"},
        )
        self.assertEqual(r.status_code, 413)

    def test_bad_host_header_rejected(self):
        r = httpx2.post(
            self.server.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {TOKEN}", "Host": "evil.example",
                     "accept": "application/json, text/event-stream"},
        )
        self.assertNotEqual(r.status_code, 200)

    # --- protocol ---
    def test_initialize_and_list_tools(self):
        tools = run(self.server.url, lambda s: s.list_tools()).tools
        by_name = {t.name: t for t in tools}
        self.assertEqual(set(by_name), {"list_sites", "search_analytics"})
        for t in tools:
            self.assertTrue(t.annotations.read_only_hint)
            self.assertFalse(t.annotations.destructive_hint)

    def test_list_sites(self):
        r = self.call("list_sites", {})
        self.assertFalse(r.is_error)
        self.assertEqual(json.loads(text(r)), {"sites": [SITE]})

    def test_valid_query_returns_data_and_sends_exact_body(self):
        r = self.call("search_analytics", {
            "site_url": SITE, "start_date": START, "end_date": END, "dimensions": ["query"],
            "filters": [{"dimension": "country", "operator": "equals", "expression": "deu"}], "row_limit": 25,
        })
        self.assertFalse(r.is_error, text(r))
        out = text(r)
        self.assertIn("urban sports club", out)
        self.assertIn("<untrusted_search_console_data>", out)
        self.assertEqual(out.count("</untrusted_search_console_data>"), 1)  # injection escaped
        call = self.server.calls()[-1]
        self.assertEqual(call["site"], SITE)
        self.assertEqual(call["body"], {
            "startDate": START, "endDate": END, "dimensions": ["query"], "type": "web", "rowLimit": 25,
            "startRow": 0,
            "dimensionFilterGroups": [{"groupType": "and", "filters": [
                {"dimension": "country", "operator": "equals", "expression": "deu"}]}],
        })

    def test_empty_result(self):
        r = self.call("search_analytics", {"site_url": SITE, "start_date": START, "end_date": END,
                                           "dimensions": ["country"]})
        self.assertFalse(r.is_error)
        self.assertIn("No data", text(r))

    # --- validation: rejected before reaching Google ---
    def _assert_rejected(self, args, expect=None):
        before = len(self.server.calls())
        r = self.call("search_analytics", {"site_url": SITE, "start_date": START, "end_date": END, **args})
        self.assertTrue(r.is_error, text(r))
        if expect:
            self.assertIn(expect, text(r))
        self.assertEqual(len(self.server.calls()), before, "invalid request reached Search Console")

    def test_reject_unlisted_site(self):
        self._assert_rejected({"site_url": "sc-domain:competitor.com"}, "not an allowed property")

    def test_reject_unknown_dimension(self):
        self._assert_rejected({"dimensions": ["password"]})

    def test_reject_unknown_operator(self):
        self._assert_rejected({"filters": [{"dimension": "query", "operator": "sql", "expression": "x"}]})

    def test_reject_extra_filter_fields(self):
        self._assert_rejected({"filters": [{"dimension": "query", "expression": "x", "groupType": "or"}]})

    def test_reject_row_limit_above_cap(self):
        self._assert_rejected({"row_limit": 6000}, "cannot exceed 5000")
        self._assert_rejected({"row_limit": 100000})

    def test_reject_future_dates(self):
        self._assert_rejected({"end_date": (TODAY + timedelta(days=5)).isoformat()}, "future")

    def test_reject_inverted_dates(self):
        self._assert_rejected({"start_date": END, "end_date": START})

    def test_reject_too_old(self):
        self._assert_rejected({"start_date": (TODAY - timedelta(days=600)).isoformat()}, "16-month")

    def test_reject_duplicate_dimensions(self):
        self._assert_rejected({"dimensions": ["query", "query"]})

    def test_reject_oversized_filter_expression(self):
        self._assert_rejected({"filters": [{"dimension": "query", "expression": "a" * 501}]})

    # --- errors never leak secrets ---
    def test_unexpected_crash_does_not_leak_secret(self):
        r = self.call("search_analytics", {"site_url": SITE, "start_date": START, "end_date": END,
                                           "dimensions": ["page"], "row_limit": 13})
        self.assertTrue(r.is_error)
        self.assertNotIn(SECRET_IN_ERROR, text(r))
        self.assertNotIn("key=", text(r))

    def test_gsc_error_message_is_safe(self):
        r = self.call("search_analytics", {"site_url": SITE, "start_date": START, "end_date": END,
                                           "dimensions": ["page"], "row_limit": 14})
        self.assertTrue(r.is_error)
        self.assertIn("no access to this property", text(r))


class RateLimitTests(unittest.TestCase):
    def test_rate_limit_returns_429(self):
        with Server(RATE_LIMIT_PER_MINUTE="5") as server:
            headers = {"Authorization": f"Bearer {TOKEN}", "accept": "application/json, text/event-stream"}
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            codes = [httpx2.post(server.url, json=body, headers=headers).status_code for _ in range(7)]
        self.assertNotIn(429, codes[:5])
        self.assertEqual(codes[5:], [429, 429])


class LoggingTests(unittest.TestCase):
    def test_logs_contain_no_query_text_or_rows(self):
        with Server() as server:
            run(server.url, lambda s: s.call_tool("search_analytics", {
                "site_url": SITE, "start_date": START, "end_date": END, "dimensions": ["query"],
                "filters": [{"dimension": "query", "operator": "contains", "expression": "VERY-PRIVATE-TERM"}],
            }))
        self.assertNotIn("VERY-PRIVATE-TERM", server.logs)
        self.assertNotIn("urban sports club", server.logs)
        self.assertNotIn(TOKEN, server.logs)


class StartupTests(unittest.TestCase):
    def test_refuses_to_start_without_token(self):
        env = {**os.environ, "MCP_TOKEN": "", "ALLOWED_SITES": SITE}
        p = subprocess.run([sys.executable, "-c", "import tests.fake_app"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("MCP_TOKEN must be set", p.stderr)


if __name__ == "__main__":
    unittest.main()
