-- Personalized "buy box": what each searcher is looking for. Drives the buy score and priority ranking.
CREATE TABLE IF NOT EXISTS buy_boxes (
    user_id             UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    industries          TEXT[] NOT NULL DEFAULT '{}',     -- industry slugs; empty = any
    states              TEXT[] NOT NULL DEFAULT '{}',     -- state codes; empty = any
    min_price           NUMERIC NOT NULL DEFAULT 500000,
    max_price           NUMERIC NOT NULL DEFAULT 5000000,
    cash_available      NUMERIC NOT NULL DEFAULT 300000,  -- what the buyer can inject (equity + closing)
    interest_rate_pct   NUMERIC NOT NULL,
    term_years          INT NOT NULL DEFAULT 10,
    target_multiple     NUMERIC,                          -- NULL = use the industry's typical multiple
    min_dscr            NUMERIC NOT NULL DEFAULT 1.25,
    buyer_salary        NUMERIC NOT NULL DEFAULT 80000,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
