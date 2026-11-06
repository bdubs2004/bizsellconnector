# Architecture

## Layout

```
migrations/001_init.sql      schema (scope Section 5 + pipeline_runs, match_review, api_keys)
migrations/002_multistate.sql core_name + denormalized score on businesses, indexes for ~6M rows
src/dealfeed/
  config.py                  env-based settings
  db.py                      psycopg connection + migration runner
  normalize.py               names, core names, addresses, dates, RA-service detection
  industries.py              v1 + backup industries: NAICS, name rules, payroll/revenue ratios
  ingest/common.py           column mapping, raw-file stash, run log, bulk COPY upsert
  ingest/states.py           state registry sources (IA CO NY CT OR TX): verified column maps, per-state
                             filters, streaming loader (csv or zip), paged Socrata downloader with retries
  states_meta.py             state names / covered-state list
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

**State registries are the spine.** Each registry entity becomes one business. Its business id equals its
`source_records.id`, a UUIDv5 of `<source>:<registry id>` (e.g. `co_sos:19911000001`). IDs are therefore
stable across rebuilds, so saved targets and API consumers don't break when the pipeline re-runs.

**One mapper per state, one shape out.** Each state in `ingest/states.py` declares its download (Socrata
dataset + a server-side `$where`, or a URL), its column aliases (API field names and display names), and a
mapper that returns a common record: normalized entity type, formation date, status
(active / delinquent), address and what kind of address it is, county, NAICS, people (name + role only),
DBA names. Rows are skipped with a counted reason (nonprofit, foreign, dissolved, too young), and the
reasons land in `pipeline_runs.notes`. Only *domestic* entities are kept, so a multi-state company appears
once, in its home state, with its real formation date.

**Scale (~5.6M businesses).** Raw rows are never copied into Postgres; the downloaded file in
`DEALFEED_RAW_DIR` is the archive, so reprocess from it if the mapping changes. A registry load writes a
slim provenance row to `source_records` and stages the mapped fields in `registry_stage`. The build applies
the stage to `businesses` and empties it, so the database holds one copy of each business, not two.
Scores are stored as a single number (`businesses.score`). The point-by-point explanation is recomputed by
`score.explain()` for the rows someone is looking at (same rules, same data). Storing it was about 20% of
the database. The spine upserts just the latest pull. Exact matching is a SQL join on
`(core_name, zip)`, and fuzzy matching runs one ZIP3 area at a time. Classification is SQL regexes.
Scoring is keyset-paged in 20k chunks written back with COPY, and `businesses.score` is denormalized so
search can use an index per (state, industry). Search counts stop at 10,000 ("10,000+").

**`source_records` holds everything.** Each row has `canon` (fields mapped to our shape and normalized at
ingest) and `raw` (the original row minus columns we deliberately don't keep). `raw` is never served.
Untouched source files are kept in `DEALFEED_RAW_DIR/<source>/<date>/` for reprocessing.

**Matching** (`match.py`) links PPP/7(a)/504 records to businesses:
1. Exact core name + ZIP, in SQL.
2. Fuzzy match (rapidfuzz `token_sort_ratio`): same ZIP first, else same city, +5 for the same street
   address. A score of 92 or more links automatically.
3. Scores from 80 to 92: an identical street address links; otherwise the record goes to Claude if
   enabled, then to `match_review`, but only for industries we sell. Anything else in that range stays
   unlinked, so the queue stays readable.
4. Two near-equal top candidates always go to review.
5. Unmatched PPP borrowers become their own business (`primary_source = 'ppp'`).

Manual decisions survive `build --rematch`, and rejected pairs are never proposed again.

**Inactive detection.** A business whose registry record wasn't in its state's latest successful pull is
marked `inactive` on the next build (scope 3.4: flagged within one cycle). Colorado and Texas also report
"delinquent" (behind on filings / franchise tax), which costs 20 points.

**Registered-agent tenure** comes from `business_people.first_seen/last_seen`, which widen with every
monthly pull. The "same agent 15+ years" signal therefore can't fire until there is history. v1 uses
"registered agent is an individual, not a commercial RA service" as a weaker owner-operated proxy.

**Classification** order: NAICS from a linked SBA loan, then NAICS from the state filing (CT, TX), then name
rules (Postgres regexes compiled from `industries.py`, `\b` → `\y`), then Claude if enabled.

**Score** (`score.py`): base 30, then signed points per signal, clamped to 0–100. Weights are in `WEIGHTS`.
Bump `MODEL_VERSION` when you change them. Each score stores its `breakdown`, so `explain_score` is exact.
Strongest negative: a 7(a)/504 loan whose `BusinessAge` is "Change of Ownership" within 6 years, which
means the business was just bought.

**Size**: the largest first-draw PPP loan → `payroll ≈ loan / 2.5 × 12`, shown as 0.8–1.25× ranges;
jobs ±25–30%; revenue from placeholder industry payroll ratios. Revenue ratios in `industries.py` need
real benchmarks before customers see them.

**Privacy rules enforced in code** (scope 2.6), all tested:
- only mapped business-level fields are stored from registry files; agent/officer/CEO addresses,
  emails and owner-demographic flags (CT) are never mapped, so they never reach the database
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

**Live status check** (`services/live.py`, `states.live_lookup`). One Socrata query by registry id when a
user asks ("Check with the state now", `check_live_status`, `/live-status`). The row goes through the same
state mapper as the bulk load, so "active" means the same thing in both places. The result is compared
with our stored name, address and status, and cached per business for 10 minutes. Failures aren't cached,
and the check never raises: a slow state shows "couldn't reach" instead of an error page. It only reports
and never writes to our tables; the monthly pull stays the single writer. Iowa's data hub can't look up
one record, so Iowa businesses show their last-pull date instead.

**Buy score and priority** (`buy.py`, `services/buybox.py`). The sell score is a property of the business,
so it's stored and indexed. The buy score depends on who's buying, so it's computed per request against the
user's buy box (or labeled defaults). It reuses `services/deal_math.py` on a rough deal: revenue range ×
industry SDE margin × multiple, all placeholders in `industries.py`. Priority is the geometric mean of the
two, so a business must score well on both. Search fetches the top 1,000 matches by sell score from the
index, scores and re-ranks them in Python (milliseconds), and computes sell explanations only for the page
being shown.
