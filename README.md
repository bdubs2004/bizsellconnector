# Deal Feed (working name): backend

Off-market small businesses in the Upper Midwest that are **not listed for sale but are likely to sell**,
scored and served to humans (web app, REST API) and AI agents (MCP server).

v1 scope: **Iowa only**, three industries (pest control, commercial cleaning, independent insurance agencies)
plus three backups (auto repair, bookkeeping/tax, landscaping).

```
raw CSVs ─► ingest ─► normalize ─► match/dedupe ─► classify ─► size ─► score ─► Postgres
                                                                                  │
                                              services/ops.py (plans, metering)
                                               │                  │                 │
                                     web app (HTMX)        REST API (FastAPI)   MCP server
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how each stage works and why.

## Quickstart (fictional sample data)

```bash
docker compose up -d                       # Postgres 16 on :5432 (also creates dealfeed_test)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,llm]"
cp .env.example .env && set -a && source .env && set +a

dealfeed migrate
dealfeed demo                              # loads tests/fixtures/*, builds, prints a demo API key
dealfeed serve                             # web app at http://localhost:8000, API docs at /docs
pytest -q                                  # 43 tests; DROPS the dealfeed_test schema each run
```

Try it:

```bash
curl -s -X POST localhost:8000/v1/targets/search \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"industry": "pest control", "min_years_in_business": 20}'
```

## Web app

Server-rendered with FastAPI + Jinja + HTMX: no JavaScript build step, same process as the API.
HTMX and Leaflet are vendored in `src/dealfeed/web/static/vendor/`, so there's no CDN dependency.

| Page | What it does |
|---|---|
| `/` | Landing page + free sample (top 3 per v1 industry, score 50+, 10+ years). No login needed. |
| `/search` | Filters (industry, counties, city, name, years, employees, min score), map, ranked table with reasons, CSV export. Filters update results in place. |
| `/business/{id}` | Score breakdown, size estimate, SBA loans, filings, sources; save to pipeline with status and notes. |
| `/pipeline` | Saved targets by status; change status/notes inline; CSV export. |
| `/deal-math` | SBA 7(a) calculator (DSCR, debt service, cash needed, verdict). |
| `/connect` | Plan and usage, create/revoke API keys, copy-paste setup for Claude Code, Claude Desktop and the REST API. |

**Auth is dev-only for now.** Signing in takes just an email (no password), and new accounts get
`DEALFEED_DEV_SIGNUP_PLAN` (default: searcher). There's a plan switcher on `/connect` so you can try every
plan. Both refuse to work when `DEALFEED_ENV=production`. Everything auth-related lives in
`src/dealfeed/web/auth.py`, so adding Clerk or Supabase later means replacing that file and the `/login`
routes. **Don't put the web app on the public internet until that's done.**

The map uses OpenStreetMap tiles and only shows businesses with coordinates (`dealfeed geocode`).
`dealfeed demo` puts the fictional businesses at approximate city centers so the map has something to show.

## Phase 1 runbook (real data)

Run this on your own machine and accounts (scope ground rule 1).

```bash
# 1. Iowa SOS (free, CC BY 4.0). Check the column mapping first:
dealfeed ingest ia-sos --download          # or: --file path/to/rows.csv
#    If it fails with "Could not find required column(s)", the error lists the real headers.
#    Add the spelling to SPEC in src/dealfeed/ingest/ia_sos.py. Same for `dealfeed inspect FILE --source ...`.

# 2. SBA PPP (download the CSVs from data.sba.gov/dataset/ppp-foia; filtered to IA on load)
dealfeed ingest ppp ~/Downloads/public_150k_plus_*.csv ~/Downloads/public_up_to_150k_*.csv

# 3. SBA 7(a) and 504 (data.sba.gov/dataset/7a-504-foia)
dealfeed ingest 7a ~/Downloads/foia-7a-fy2020-present.csv
dealfeed ingest 504 ~/Downloads/foia-504-fy2010-present.csv

# 4. Build: match -> classify -> size -> score (idempotent; re-run any time)
dealfeed build -v
dealfeed stats                             # match methods, industry counts, % with size data
dealfeed review list                       # gray-zone matches waiting for you
dealfeed review accept 12                  # or: reject 12
dealfeed geocode --limit 50                # Census geocoder for lat/lon + county (try small first)

# 5. The Phase 1 deliverable: top 100 per industry as a spreadsheet
dealfeed export-top --industry "pest control" --limit 100 --out pest_control_top100.csv
```

Optional: `DEALFEED_LLM=on` + `ANTHROPIC_API_KEY` sends gray-zone matches to Claude Haiku before the manual
queue, and `dealfeed build --llm-classify 500` classifies unlabeled businesses by name. Spot-check 200
classifications by hand before trusting them (scope 3.2 step 4).

## Accounts and keys

```bash
dealfeed user create you@example.com --plan fund
dealfeed user key you@example.com          # prints a dfk_... key once; only its hash is stored
dealfeed user usage you@example.com
```

Plans (`services/accounts.py`, from scope Section 9): free (10 per call, 50 rows/month, no MCP, no export),
searcher (1,000 rows/month), fund (5,000), api (metered, 100k abuse cap). Per-call results are capped at 50.

## Connect it to Claude

**Claude Desktop / Claude Code (local, stdio).** Runs as `DEALFEED_LOCAL_USER`:

```json
{
  "mcpServers": {
    "dealfeed": {
      "command": "/path/to/.venv/bin/dealfeed",
      "args": ["serve-mcp", "--transport", "stdio"],
      "env": {
        "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/dealfeed",
        "DEALFEED_LOCAL_USER": "you@example.com"
      }
    }
  }
}
```

**Hosted (Streamable HTTP).** `dealfeed serve-mcp --transport http --port 8001`. In v1 the bearer token is a
Deal Feed API key, which works with clients that let you set a header, for example
`claude mcp add --transport http dealfeed https://your-host/mcp --header "Authorization: Bearer dfk_..."`.
claude.ai and ChatGPT connectors need a real OAuth login flow; that's Phase 3 (see below).

## Tools / endpoints

| MCP tool | REST | Notes |
|---|---|---|
| `search_targets` | `POST /v1/targets/search` | ranked by score; filters: industry/NAICS, counties, city, years, employees, payroll, min score |
| `get_business` | `GET /v1/businesses/{id}` | every field with source + date; never returns raw records |
| `explain_score` | `GET /v1/businesses/{id}/score` | point-by-point |
| `sba_deal_math` | `POST /v1/deal-math` | interest rate is always an input |
| `market_summary` | `GET /v1/markets/summary` | counts, age/size/score distributions, counties, 7(a) benchmarks |
| `save_target` / `list_saved_targets` | `POST/GET/DELETE /v1/saved-targets` | pipeline statuses: new, researching, contacted, passed |
| `export_targets` | `POST /v1/exports` | CSV; search exports count toward monthly rows |
| | `GET /v1/me` | plan + usage |

## What's verified and what isn't

Verified here: the full pipeline, REST API, MCP (in-process and live over HTTP with bearer auth), plan limits,
privacy rules, and the web app (tests plus a real-browser run of every page at desktop and phone widths).
All of it ran against the fictional fixtures and Postgres 16.

**Not verified yet** (the build environment couldn't reach these hosts):
- **Iowa SOS column headers.** The mapping accepts several spellings, but the real file may use another one.
  The first `ingest ia-sos` will tell you.
- **Census geocoder** request/response format (`geocode.py`). Run it with `--limit 50` and check the output.
- Real-world match rates and score quality. That's the Phase 1 hand-check: 100 matches, 200
  classifications, 30 targets.

## Phase 3 TODO (not built yet)

- Real auth (Clerk or Supabase): replace `web/auth.py` for the web app, and swap `ApiKeyVerifier` in
  `mcp_server.py` for a JWT verifier so claude.ai/ChatGPT connectors can log in
- Set `DEALFEED_SESSION_SECRET` and `DEALFEED_ENV=production` when hosting (secure cookies, dev login off)
- Connection pooling (`psycopg_pool`), Sentry, scheduled refresh (GitHub Actions or Railway cron)
- Raw-file storage on R2/S3 instead of a local folder
- Stripe subscriptions and metered billing (the `api_usage` table already records rows per call)
- Website-enrichment signals (`score.py` already reads `stale_website` / `no_website` from `signals`)
- Census CBP coverage metric
- Map tiles: OpenStreetMap's public tiles are fine for light use; switch to a paid tile provider at scale
