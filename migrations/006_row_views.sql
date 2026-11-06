-- Fair metering: a business counts against a user's row quota once per month, the first time they see it.
-- An AI re-running or paging through overlapping searches doesn't burn quota on repeats.
CREATE TABLE IF NOT EXISTS row_views (
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month        DATE NOT NULL,
    business_id  UUID NOT NULL,
    PRIMARY KEY (user_id, month, business_id)
);
