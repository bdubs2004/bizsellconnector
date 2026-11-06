-- Multi-state scale-up (~10M businesses).
-- * core_name on businesses so exact matching runs in SQL
-- * score denormalized onto businesses so search can use one index per (state, industry)
-- * address_kind / status_since from state registries

ALTER TABLE businesses ADD COLUMN IF NOT EXISTS core_name     TEXT;
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS score         INT;
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS address_kind  TEXT;     -- principal, location, service_of_process
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS status_since  DATE;     -- e.g. delinquent since

CREATE INDEX IF NOT EXISTS businesses_core_zip         ON businesses (core_name, zip);
CREATE INDEX IF NOT EXISTS businesses_zip3             ON businesses (left(zip, 3));
CREATE INDEX IF NOT EXISTS businesses_state_score      ON businesses (state, score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS businesses_state_naics_score ON businesses (state, naics_code, score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS businesses_naics_score      ON businesses (naics_code, score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS source_records_pending      ON source_records (source) WHERE match_method IS NULL;
CREATE INDEX IF NOT EXISTS business_people_business    ON business_people (business_id);
CREATE INDEX IF NOT EXISTS sba_loans_state_naics       ON sba_loans (state, naics_code);
