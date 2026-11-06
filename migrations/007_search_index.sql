-- Searches across all states ("everything", or industry-free AI queries) rank by score without a state
-- prefix; give them their own index (partial: suppressed/inactive rows are never searched).
CREATE INDEX IF NOT EXISTS businesses_score_live
    ON businesses (score DESC NULLS LAST, formation_date, id)
 WHERE NOT suppressed AND status <> 'inactive';
