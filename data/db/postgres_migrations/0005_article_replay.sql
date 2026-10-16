-- Durable date identity and immutable replay payloads. Legacy pairs are not guessed.
CREATE TABLE IF NOT EXISTS article_work (
    article_id UUID NOT NULL REFERENCES articles(article_id) ON DELETE CASCADE,
    company_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    run_date DATE NOT NULL,
    snapshot JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','error','processed','redundant','filtered')),
    PRIMARY KEY (article_id, company_id)
);
CREATE TABLE IF NOT EXISTS article_aggregate_work (
    run_date DATE NOT NULL,
    dimension TEXT NOT NULL,
    dimension_key TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    evidence UUID[] NOT NULL,
    PRIMARY KEY (run_date, dimension, dimension_key, evidence_hash)
);

CREATE TABLE IF NOT EXISTS article_fact_inputs (
    fact_id UUID PRIMARY KEY REFERENCES company_facts(fact_id) ON DELETE CASCADE,
    run_date DATE NOT NULL,
    snapshot JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_article_fact_inputs_date ON article_fact_inputs (run_date);
CREATE TABLE IF NOT EXISTS article_aggregate_inputs (
    run_date DATE NOT NULL,
    dimension TEXT NOT NULL,
    dimension_key TEXT NOT NULL,
    fact_id UUID NOT NULL REFERENCES company_facts(fact_id) ON DELETE CASCADE,
    evidence_hash TEXT NOT NULL,
    PRIMARY KEY (run_date, dimension, dimension_key, fact_id),
    FOREIGN KEY (run_date, dimension, dimension_key, evidence_hash)
        REFERENCES article_aggregate_work(run_date, dimension, dimension_key, evidence_hash)
        ON DELETE CASCADE
);
