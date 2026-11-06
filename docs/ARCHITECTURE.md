# Architecture

## Layout

```
migrations/001_init.sql      schema (scope Section 5 + pipeline_runs, match_review, api_keys)
src/dealfeed/
  config.py                  env-based settings
  db.py                      psycopg connection + migration runner
  normalize.py               names, core names, addresses, dates, RA-service detection
  industries.py              v1 + backup industries: NAICS, name rules, payroll/revenue ratios
  ingest/common.py           column mapping, raw-file stash, run log, bulk COPY upsert
  ingest/ia_sos.py           Iowa SOS loader (drops registered-agent address columns)
  ingest/sba.py              PPP / 7(a) / 504 loaders (state filter, drops demographics, skips sole props)
  match.py                   spine build, loan-record linking, review queue
  classify.py                NAICS from loans > name rules > Claude (optional)
  size.py                    PPP payroll/headcount/revenue ranges
  score.py                   rules-v1 score with per-signal reasons
  geocode.py                 Census batch geocoder + Iowa county FIPS table
  llm.py                     optional Claude Haiku calls (structured outputs)
  pipeline.py                build() and stats()
  services/
    accounts.py              users, hashed API keys, plans, metering
    search.py                search_targets, market_summary
    business.py              get_business, explain_score
    targets.py               saved pipeline, CSV export
    deal_math.py             SBA 7(a) scenario math (pure)
    ops.py                   the single entry point both surfaces call
  api/app.py                 FastAPI app: REST under /v1, plus mounts the web app
  web/routes.py              server-rendered pages (Jinja + HTMX partials)
  web/auth.py                web sign-in seam (dev-only email login for now)
  web/templates/, static/    HTML, CSS, vendored htmx 2.0.4 + Leaflet 1.9.4
  mcp_server.py              MCP server (stdio + Streamable HTTP with bearer auth)
  cli.py                     `dealfeed` command
tests/                       43 tests on fictional fixtures
```

## Key decisions

**Iowa SOS is the spine.** Each SOS entity becomes one business. Its business id equals its
`source_records.id`, a UUIDv5 of `ia_sos:<corp number>`. IDs are therefore stable across rebuilds,
so saved targets and API consumers don't break when the pipeline re-runs.

**`source_records` holds everything.** Each row has `canon` (fields mapped to our shape and normalized at
ingest) and `raw` (the original row minus columns we deliberately don't keep). `raw` is never served.
Untouched source files are kept in `DEALFEED_RAW_DIR/<source>/<date>/` for reprocessing.

**Matching** (`match.py`) links PPP/7(a)/504 records to businesses:
exact core-name+ZIP → fuzzy (rapidfuzz `token_sort_ratio`, same ZIP, else same city, +5 for same street
address) → ≥92 auto-link, 80–92 to Claude if enabled, then to `match_review`. Two near-equal top candidates
always go to review. Unmatched PPP borrowers become their own business (`primary_source = 'ppp'`). Manual
decisions survive `build --rematch`, and rejected pairs are never proposed again.

**Inactive detection.** A business whose SOS record wasn't in the latest successful `ia_sos` run is marked
`inactive` on the next build (scope 3.4: flagged within one cycle).

**Registered-agent tenure** comes from `business_people.first_seen/last_seen`, which widen with every
monthly pull. The "same agent 15+ years" signal therefore can't fire until there is history. v1 uses
"registered agent is an individual, not a commercial RA service" as a weaker owner-operated proxy.

**Score** (`score.py`): base 30, then signed points per signal, clamped to 0–100. Weights are in `WEIGHTS`.
Bump `MODEL_VERSION` when you change them. Each score stores its `breakdown`, so `explain_score` is exact.
Strongest negative: a 7(a)/504 loan whose `BusinessAge` is "Change of Ownership" within 6 years, which
means the business was just bought.

**Size**: the largest first-draw PPP loan → `payroll ≈ loan / 2.5 × 12`, shown as 0.8–1.25× ranges;
jobs ±25–30%; revenue from placeholder industry payroll ratios. Revenue ratios in `industries.py` need
real benchmarks before customers see them.

**Privacy rules enforced in code** (scope 2.6), all tested:
- registered-agent address columns are dropped at ingest (often the owner's home)
- PPP race/ethnicity/gender/veteran columns are dropped
- PPP loans to sole proprietors, self-employed individuals and independent contractors are skipped
- only names from public filings are exposed, with role and source

**One ops layer.** REST and MCP both go through `services/ops.py`, so plan gates, per-call caps,
monthly row quotas and `api_usage` metering behave identically. MCP tools are async: they read the caller
from the auth context and then run DB work in a worker thread.

**Web app.** Every page is a plain HTML form that works without JavaScript. When HTMX is loaded, requests
carry `HX-Request` and get back just a fragment (`templates/partials/`): search results, the save box,
a pipeline row, a deal-math result. Pages call `Ops(conn, user, "web")`, so plan limits apply there too.
Sessions are signed cookies (Starlette `SessionMiddleware`, SameSite=Lax, Secure in production). Login
`next=` only accepts local paths, and map JSON escapes `<` so a business name can't break out of its
`<script>` tag.
