-- Credits model (how Apollo / ZoomInfo meter): searching and viewing inside the product is free under a
-- daily fair-use cap; credits are spent when data leaves the platform (CSV exports, REST API pulls).
-- A business costs one credit per user per month, however many times it's exported or pulled.

-- Views: when a business was first seen this month (daily fair-use counts new views per day).
ALTER TABLE row_views ADD COLUMN IF NOT EXISTS first_day DATE NOT NULL DEFAULT current_date;
CREATE INDEX IF NOT EXISTS row_views_user_day ON row_views (user_id, first_day);

CREATE TABLE IF NOT EXISTS credit_charges (
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month        DATE NOT NULL,
    business_id  UUID NOT NULL,
    kind         TEXT NOT NULL,              -- export, api
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, month, business_id)
);

-- Top-ups (bought or granted). They don't expire; each month's plan credits are used first.
CREATE TABLE IF NOT EXISTS credit_grants (
    id       BIGSERIAL PRIMARY KEY,
    user_id  UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount   INT NOT NULL CHECK (amount > 0),
    reason   TEXT,
    at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
