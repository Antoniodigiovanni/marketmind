CREATE TABLE IF NOT EXISTS financials (
    financial_id    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      UUID        NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    taxonomy        TEXT        NOT NULL,
    concept         TEXT        NOT NULL,
    unit            TEXT        NOT NULL,
    fiscal_year     INTEGER,
    fiscal_period   TEXT,
    period_start    DATE,
    period_end      DATE        NOT NULL,
    filed_at        DATE        NOT NULL,
    accession_number TEXT       NOT NULL,
    form_type       TEXT,
    frame           TEXT,
    value           NUMERIC     NOT NULL,
    source          TEXT        NOT NULL DEFAULT 'sec-companyfacts',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_financials_observation
    ON financials (
        company_id, taxonomy, concept, unit, period_start, period_end,
        filed_at, accession_number
    ) NULLS NOT DISTINCT;

CREATE INDEX IF NOT EXISTS idx_financials_pit
    ON financials (company_id, concept, period_end, filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_financials_accession
    ON financials (accession_number);
