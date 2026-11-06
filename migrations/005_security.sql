-- Removal requests: a business (or its owner) can ask to be left out. Suppressed businesses disappear from
-- search, profiles, exports, pipelines, market counts and AI tools. Survives rebuilds (the spine never
-- touches this column).
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS suppressed BOOLEAN NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS suppression_log (
    id           BIGSERIAL PRIMARY KEY,
    business_id  UUID NOT NULL,
    action       TEXT NOT NULL,           -- suppress, unsuppress
    reason       TEXT,
    at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- API keys: when each key was last used (helps spot leaked/abused keys).
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;
