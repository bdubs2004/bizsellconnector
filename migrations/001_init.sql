-- Deal Feed v1 schema. Follows Section 5 of the scope doc, plus a few
-- operational tables (pipeline_runs, match_review, api_keys) the pipeline
-- and API need.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Pipeline bookkeeping
-- ---------------------------------------------------------------------------

-- One row per ingest of one raw file (scope 3.2 step 1: log source, date, row counts)
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,          -- ia_sos, ppp, sba_7a, sba_504
    file_path       TEXT,
    file_sha256     TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running',  -- running, ok, failed
    rows_read       INT,
    rows_loaded     INT,
    notes           TEXT
);

-- ---------------------------------------------------------------------------
-- Core business data
-- ---------------------------------------------------------------------------

-- One row per real-world business
CREATE TABLE IF NOT EXISTS businesses (
    id                  UUID PRIMARY KEY,
    legal_name          TEXT NOT NULL,
    normalized_name     TEXT NOT NULL,
    dba_names           TEXT[],
    entity_type         TEXT,
    state_of_formation  TEXT,
    formation_date      DATE,
    status              TEXT NOT NULL DEFAULT 'unknown',   -- active, inactive, unknown
    naics_code          TEXT,
    naics_confidence    REAL,
    naics_source        TEXT,           -- ppp, sba, rules, llm, manual
    address_line1       TEXT,
    city                TEXT,
    state               TEXT,
    zip                 TEXT,
    county              TEXT,
    latitude            REAL,
    longitude           REAL,
    website             TEXT,
    primary_source      TEXT,           -- which source created this business
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

-- Every raw record from every source, linked to a business.
-- `canon` holds the fields mapped into our standard shape; `raw` holds the
-- original row MINUS fields we deliberately do not keep (see ingest/*.py).
-- `raw` is never served through the API.
CREATE TABLE IF NOT EXISTS source_records (
    id              UUID PRIMARY KEY,
    business_id     UUID REFERENCES businesses(id) ON DELETE SET NULL,
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    pulled_at       TIMESTAMPTZ NOT NULL,
    first_seen_at   TIMESTAMPTZ NOT NULL,
    run_id          BIGINT REFERENCES pipeline_runs(id),
    match_method    TEXT,               -- spine, exact, fuzzy, llm, manual, new
    match_score     REAL,
    canon           JSONB NOT NULL,
    raw             JSONB NOT NULL,
    UNIQUE (source, source_id)
);

-- Gray-zone matches waiting for a human (scope 3.2 step 3.4)
CREATE TABLE IF NOT EXISTS match_review (
    id                  BIGSERIAL PRIMARY KEY,
    source_record_id    UUID NOT NULL REFERENCES source_records(id) ON DELETE CASCADE,
    candidate_business  UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    score               REAL NOT NULL,
    llm_verdict         TEXT,           -- yes, no, unsure, NULL if LLM not used
    status              TEXT NOT NULL DEFAULT 'pending',   -- pending, matched, rejected
    created_at          TIMESTAMPTZ DEFAULT now(),
    resolved_at         TIMESTAMPTZ,
    UNIQUE (source_record_id, candidate_business)
);

-- Public officers/agents from state filings (business context only; no addresses)
CREATE TABLE IF NOT EXISTS business_people (
    id              UUID PRIMARY KEY,
    business_id     UUID REFERENCES businesses(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL,
    first_seen      DATE,
    last_seen       DATE,
    source          TEXT NOT NULL,
    UNIQUE (business_id, name, role, source)
);

CREATE TABLE IF NOT EXISTS size_estimates (
    id                  UUID PRIMARY KEY,
    business_id         UUID REFERENCES businesses(id) ON DELETE CASCADE,
    employees_low       INT,
    employees_high      INT,
    payroll_low         NUMERIC,
    payroll_high        NUMERIC,
    revenue_low         NUMERIC,
    revenue_high        NUMERIC,
    method              TEXT NOT NULL,
    as_of               DATE,
    source              TEXT NOT NULL,
    UNIQUE (business_id, method, source)
);

CREATE TABLE IF NOT EXISTS signals (
    id              UUID PRIMARY KEY,
    business_id     UUID REFERENCES businesses(id) ON DELETE CASCADE,
    signal_type     TEXT NOT NULL,
    value           JSONB,
    weight          REAL,
    observed_at     TIMESTAMPTZ,
    UNIQUE (business_id, signal_type)
);

CREATE TABLE IF NOT EXISTS scores (
    business_id     UUID PRIMARY KEY REFERENCES businesses(id) ON DELETE CASCADE,
    score           INT NOT NULL,
    reasons         TEXT[] NOT NULL,
    breakdown       JSONB NOT NULL DEFAULT '[]',   -- [{signal, points, reason}]
    model_version   TEXT NOT NULL,
    scored_at       TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS sba_loans (
    id                  UUID PRIMARY KEY,
    business_id         UUID REFERENCES businesses(id) ON DELETE SET NULL,
    source_record_id    UUID UNIQUE REFERENCES source_records(id) ON DELETE CASCADE,
    program             TEXT NOT NULL,      -- 7a, 504, ppp
    amount              NUMERIC,
    approval_date       DATE,
    lender              TEXT,
    status              TEXT,
    jobs_reported       INT,
    naics_code          TEXT,
    business_age_desc   TEXT,               -- e.g. "Change of Ownership"
    county              TEXT,
    state               TEXT
);

-- ---------------------------------------------------------------------------
-- Customers
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY,
    email           TEXT UNIQUE NOT NULL,
    plan            TEXT NOT NULL DEFAULT 'free',   -- free, searcher, fund, api
    stripe_customer TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- API keys are stored hashed; only the prefix is kept in clear for display.
CREATE TABLE IF NOT EXISTS api_keys (
    id              UUID PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_prefix      TEXT NOT NULL,
    key_hash        TEXT UNIQUE NOT NULL,
    label           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    revoked_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS saved_targets (
    user_id         UUID REFERENCES users(id) ON DELETE CASCADE,
    business_id     UUID REFERENCES businesses(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'new',   -- new, researching, contacted, passed
    notes           TEXT,
    saved_at        TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_id, business_id)
);

CREATE TABLE IF NOT EXISTS api_usage (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID REFERENCES users(id) ON DELETE CASCADE,
    tool            TEXT NOT NULL,
    surface         TEXT NOT NULL DEFAULT 'api',   -- api, mcp, web
    called_at       TIMESTAMPTZ DEFAULT now(),
    rows_returned   INT NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Indexes (scope Section 5)
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS businesses_state_naics   ON businesses (state, naics_code);
CREATE INDEX IF NOT EXISTS businesses_name_zip      ON businesses (normalized_name, zip);
CREATE INDEX IF NOT EXISTS businesses_name_trgm     ON businesses USING gin (normalized_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS scores_score_desc        ON scores (score DESC);
CREATE INDEX IF NOT EXISTS source_records_business  ON source_records (business_id);
CREATE INDEX IF NOT EXISTS source_records_src_run   ON source_records (source, run_id);
CREATE INDEX IF NOT EXISTS sba_loans_business       ON sba_loans (business_id);
CREATE INDEX IF NOT EXISTS size_estimates_business  ON size_estimates (business_id);
CREATE INDEX IF NOT EXISTS api_usage_user_month     ON api_usage (user_id, called_at);
