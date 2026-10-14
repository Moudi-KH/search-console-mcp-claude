# Google Search Console MCP server for Claude (hardened)

A read-only MCP server that lets Claude (claude.ai custom connector) query Google Search Console
search analytics. It's a hardened rework of
[garethcull/search-console-mcp](https://github.com/garethcull/search-console-mcp) that relies only on
Claude: there is no Gemini or any other AI service in the request path.

## How it works

```
Claude ──POST /mcp (Bearer token)──► Cloud Run
                                       ├─ 1. token check (constant-time) + rate limit
                                       ├─ 2. argument validation (schema + allowlists)
                                       ├─ 3. searchAnalytics.query (read-only scope)
                                       └─ 4. results wrapped as untrusted data
Claude ◄──────────── result ───────────┘
```

Tools:

| Tool | Purpose |
|---|---|
| `list_sites` | Returns the properties configured in `ALLOWED_SITES` (no API call) |
| `search_analytics` | Clicks, impressions, CTR and position by query/page/country/device/date/searchAppearance, with filters |

Both tools are annotated `readOnlyHint: true`, `destructiveHint: false`.

## Security changes vs. the original (high and medium findings)

| # | Finding (original) | Fix |
|---|---|---|
| 1 | **High**: Gemini API key leaked in error messages (`?key=` in URL, `str(e)` returned to client) | Gemini removed entirely. Errors returned to the client are fixed, generic messages; the SDK also hides unexpected exception text. Google's own 400 validation message is passed through (truncated) so Claude can fix a bad query. |
| 2 | **Medium**: every question sent to Gemini | Removed. Claude builds the query and sends structured parameters. `GEMINI_API_KEY` no longer exists. |
| 3 | **Medium**: Werkzeug 2.3.7 / requests 2.31.0 with known CVEs | Flask/Werkzeug no longer used (official MCP SDK + uvicorn). `requests>=2.32.4`. Lock with hashes before production (see `requirements.txt`). |
| 4 | **Medium**: public search text passed to the AI unfiltered (indirect prompt injection) | Rows are wrapped in `<untrusted_search_console_data>`; `<` is escaped so row text cannot close the wrapper; long strings truncated to 300 chars; server instructions tell Claude to treat rows as data. The user's question and instruction-style text are no longer echoed. All inputs are validated against allowlists before reaching Google. |
| 5 | **Medium**: no rate limiting | 60 authenticated requests/minute per instance (`RATE_LIMIT_PER_MINUTE`), HTTP 429 above that. Row limit capped at 5,000 (`MAX_ROW_LIMIT`). Set Cloud Run `--max-instances` as a hard ceiling. |

Also addressed: auth is checked before the body is parsed; 64 KB request body limit; Host header
allowlist (DNS-rebinding protection); no query text or result rows in logs; the server refuses to
start without a strong token (≥32 chars) and at least one allowed site; impression-weighted totals
and correct column labels; no crash on empty results; non-root container.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `MCP_TOKEN` | yes | Shared secret, ≥32 chars. Generate: `openssl rand -base64 48` |
| `ALLOWED_SITES` | yes | Comma-separated properties, e.g. `sc-domain:urbansportsclub.com` |
| `ALLOWED_HOSTS` | recommended | Comma-separated Host headers, e.g. `gsc-mcp-xxxx-ey.a.run.app` |
| `RATE_LIMIT_PER_MINUTE` | no | Default 60 |
| `MAX_ROW_LIMIT` | no | Default 5000 |
| `SEARCH_CONSOLE_KEY` | no | Base64 service-account JSON key. Leave unset to use the Cloud Run service account (keyless, preferred). |

## Deploy to Cloud Run

```bash
PROJECT=your-project
REGION=europe-west3
SA=gsc-mcp-runtime@$PROJECT.iam.gserviceaccount.com

gcloud services enable searchconsole.googleapis.com run.googleapis.com secretmanager.googleapis.com
gcloud iam service-accounts create gsc-mcp-runtime          # grant NO IAM roles

openssl rand -base64 48 | tr -d '\n' | gcloud secrets create gsc-mcp-token --data-file=-
gcloud secrets add-iam-policy-binding gsc-mcp-token --member="serviceAccount:$SA" \
  --role=roles/secretmanager.secretAccessor

gcloud run deploy gsc-mcp --source . --region $REGION \
  --service-account $SA \
  --allow-unauthenticated \
  --max-instances 2 \
  --set-secrets MCP_TOKEN=gsc-mcp-token:latest \
  --set-env-vars ALLOWED_SITES=sc-domain:urbansportsclub.com
```

`--allow-unauthenticated` is required because claude.ai cannot present Google IAM credentials; the
bearer token is enforced by the app. After the first deploy, add the service URL's host to
`ALLOWED_HOSTS` and redeploy.

In Search Console → Settings → Users and permissions, add `$SA` with **Restricted** permission.

> **Keyless auth needs a staging check.** The server first tries the Cloud Run identity with the
> `webmasters.readonly` scope. If Search Console returns 403 with that identity, create a key for
> the same service account, store it base64-encoded in Secret Manager and pass it as
> `SEARCH_CONSOLE_KEY`.

## Add to Claude

The claude.ai organization Owner adds it under **Organization settings → Connectors → Add → Custom → Web**:

- **URL:** `https://<cloud-run-host>/mcp`
- **Authentication:** No sign in
- **Request headers:** `Authorization: Bearer <MCP_TOKEN>`

## Tests

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t . -v
```

There are 43 tests, and none need Google credentials (Search Console is faked):

- **Unit tests:** config fail-closed behavior, output formatting, the injection-wrapper escape, error mapping in the Google client, the auth and rate-limit middleware.
- **Protocol tests:** a real uvicorn server, driven by the official MCP client SDK. They cover:
  - auth rejects (missing token, wrong token, unauthenticated garbage body);
  - the 64 KB body limit, Host and Origin checks;
  - `initialize` and `tools/list`;
  - the exact request body sent to Google;
  - every validation reject: site allowlist, dimensions, operators, extra fields, row cap, future/inverted/too-old dates, duplicates, long expressions;
  - no secret leakage on crashes;
  - rate limiting;
  - no query text in logs;
  - refusing to start without a token.

**Not yet verified:**

- A real Search Console call. It needs credentials.
- Keyless auth on Cloud Run.
- A live claude.ai connection.

Do these in a staging deploy: compare a few answers with the Search Console UI.

Recommended before production:

- `pip-audit -r requirements.txt` and `bandit -r gsc_mcp`
- An Aikido scan
- Hash-locking the dependencies

## Remaining accepted risks

- **One shared token.** Anyone holding it can read the allowed properties. Store it only in Secret Manager and the claude.ai connector settings, and rotate it periodically.
- **No per-person permissions.** Everyone using the connector sees the same properties.
- **The rate limit is per instance.** Total capacity is the rate limit × `--max-instances`.
