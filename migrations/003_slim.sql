-- Slim storage for ~5.6M businesses (measured: ~3.2 KB per business before this migration).
--
-- 1. Registry rows no longer keep a JSON copy of their fields in source_records.canon. New pulls are
--    staged in registry_stage, the build applies them to `businesses`, then empties the stage.
--    The downloaded file in DEALFEED_RAW_DIR stays the archive for reprocessing.
-- 2. No stored score explanations: businesses.score is kept for ranking, and the point-by-point
--    breakdown is recomputed from the same rules when someone looks at a business (score.explain).
-- 3. No raw CSV rows in source_records.raw (loan files are archived on disk too).
-- 4. Redundant indexes / surrogate keys dropped.

CREATE TABLE IF NOT EXISTS registry_stage (
    id      UUID PRIMARY KEY,            -- = source_records.id = businesses.id
    run_id  BIGINT NOT NULL,
    canon   JSONB NOT NULL
);

-- NAICS from the state filing (CT, TX) lives on the business now, not in the JSON copy.
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS filing_naics TEXT;
UPDATE businesses b SET filing_naics = sr.canon->>'naics'
  FROM source_records sr
 WHERE sr.id = b.id AND sr.canon ? 'naics' AND sr.canon->>'naics' IS NOT NULL;

-- Registry rows: drop the JSON copy (already applied to businesses). Loan rows keep canon (matching
-- reads it), but no raw row for anyone.
UPDATE source_records SET canon = '{}'::jsonb
 WHERE source NOT IN ('ppp', 'sba_7a', 'sba_504') AND canon <> '{}'::jsonb;
UPDATE source_records SET raw = '{}'::jsonb WHERE raw <> '{}'::jsonb;

-- PPP draw (first/second) moves to sba_loans so size estimates don't need the JSON.
ALTER TABLE sba_loans ADD COLUMN IF NOT EXISTS draw TEXT;
UPDATE sba_loans l SET draw = sr.canon->>'draw' FROM source_records sr
 WHERE sr.id = l.source_record_id AND l.program = 'ppp';

DROP TABLE IF EXISTS scores;

DROP INDEX IF EXISTS businesses_name_zip;          -- unused: matching uses (core_name, zip)
DROP INDEX IF EXISTS business_people_business;     -- covered by the unique key, which starts with business_id
ALTER TABLE business_people DROP CONSTRAINT IF EXISTS business_people_pkey;
ALTER TABLE business_people DROP COLUMN IF EXISTS id;

-- Space freed by the UPDATEs above is reused by later writes. To hand it back to the OS right away,
-- run once outside a transaction:  VACUUM FULL source_records, businesses;
