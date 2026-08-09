-- TRANSITIONAL: this file does not yet replace data/db/schema.sql.
-- The DuckDB schema remains authoritative until the application code is
-- ported in B1b. Verified against pgvector/pgvector:pg16 on 2026-08-09:
-- 30 tables, 72 indexes, 18 check constraints, idempotent across two applies.

-- marketmind — PostgreSQL schema (draft for review)
-- Target: PostgreSQL 16+ with the `vector` extension (pgvector).
-- Greenfield: no migration path, no legacy compatibility.
--
-- Identity model
-- ──────────────
-- `companies.company_id` is the ONLY stable entity key. Tickers, ISINs, CUSIPs,
-- LEIs and provider symbols are all time-ranged attributes in
-- `company_identifiers` / `listings`, never primary keys. This survives ticker
-- renames and — more importantly — ticker REUSE, where a delisted symbol is
-- reassigned to an unrelated company.
--
-- Every resolution from a human-facing identifier to a company_id MUST be
-- as-of dated. Resolving without a date is the same class of error as querying
-- facts without a point-in-time filter.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- news title/summary search


-- ══ Identity ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS companies (
    company_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT        NOT NULL,
    country      TEXT,
    sector       TEXT,
    description  TEXT,                    -- cached Wikipedia extract
    description_source TEXT,              -- 'wikipedia' | NULL
    first_seen   DATE        NOT NULL,
    last_seen    DATE,                    -- NULL = still tracked
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_companies_sector  ON companies (sector);
CREATE INDEX IF NOT EXISTS idx_companies_country ON companies (country);
CREATE INDEX IF NOT EXISTS idx_companies_name_trgm ON companies USING gin (name gin_trgm_ops);


-- Time-ranged identifiers. Replaces symbol_map, the planned lei_map, and the
-- delisted-ticker log. A (id_type, id_value) pair may point at different
-- companies over disjoint validity windows — that is the reuse case.
CREATE TABLE IF NOT EXISTS company_identifiers (
    identifier_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id    UUID        NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    id_type       TEXT        NOT NULL
        CHECK (id_type IN ('ticker','isin','cusip','lei','cik','figi','ric')),
    id_value      TEXT        NOT NULL,
    valid_from    DATE        NOT NULL,
    valid_until   DATE,                   -- NULL = current
    is_primary    BOOLEAN     NOT NULL DEFAULT FALSE,
    source        TEXT        NOT NULL,   -- 'universe-csv' | 'openfigi' | 'gleif' | 'manual'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT identifier_window_valid CHECK (valid_until IS NULL OR valid_until > valid_from)
);

-- One live holder of a given identifier at a time.
CREATE UNIQUE INDEX IF NOT EXISTS uq_identifier_current
    ON company_identifiers (id_type, id_value)
    WHERE valid_until IS NULL;

CREATE INDEX IF NOT EXISTS idx_identifiers_lookup  ON company_identifiers (id_type, id_value, valid_from, valid_until);
CREATE INDEX IF NOT EXISTS idx_identifiers_company ON company_identifiers (company_id, id_type);

-- Manually curated rows must never be overwritten by an enrichment job.
CREATE INDEX IF NOT EXISTS idx_identifiers_source  ON company_identifiers (source);


-- A company may have several tradable lines (dual listings, ADRs). Prices,
-- dividends and short interest hang off the LISTING, not the company, because
-- currency and exchange calendar are properties of the listing.
CREATE TABLE IF NOT EXISTS listings (
    listing_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id   UUID        NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    provider     TEXT        NOT NULL DEFAULT 'yahoo',
    symbol       TEXT        NOT NULL,    -- provider symbol, e.g. 'ASML.AS'
    exchange     TEXT,                    -- e.g. 'XAMS'
    -- How the PROVIDER quotes this line, so the loader knows what to normalise.
    -- The authoritative currency of any given bar lives on prices.currency:
    -- listings is time-ranged, so putting currency here would force a
    -- redenomination to mint a new listing_id and split the price series.
    quote_convention TEXT    NOT NULL DEFAULT 'major'
        CHECK (quote_convention IN ('major', 'minor')),  -- 'minor' = pence/cents
    is_primary   BOOLEAN     NOT NULL DEFAULT TRUE,
    valid_from   DATE        NOT NULL,
    valid_until  DATE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT listing_window_valid CHECK (valid_until IS NULL OR valid_until > valid_from)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_listing_current
    ON listings (provider, symbol)
    WHERE valid_until IS NULL;

CREATE INDEX IF NOT EXISTS idx_listings_company ON listings (company_id);


-- ══ Universe ═══════════════════════════════════════════════════════════════
-- Point-in-time constituent membership. Replaces the CSV as the query surface;
-- the CSVs remain the fetch staging artefact.

CREATE TABLE IF NOT EXISTS universe_members (
    universe    TEXT NOT NULL,            -- 'sp500' | 'eurostoxx600'
    company_id  UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    added_date  DATE NOT NULL,
    removed_date DATE,                    -- NULL = still a member
    weight      DOUBLE PRECISION,
    PRIMARY KEY (universe, company_id, added_date),
    CONSTRAINT universe_window_valid CHECK (removed_date IS NULL OR removed_date > added_date)
);

CREATE INDEX IF NOT EXISTS idx_universe_asof ON universe_members (universe, added_date, removed_date);


-- ══ Market data ════════════════════════════════════════════════════════════

-- Every bar is self-describing: currency lives on the row, so a
-- redenomination is just a change in later rows rather than a new listing_id
-- and a fragmented series. Minor-unit quotes (GBp) are normalised to the
-- major unit at load time, so currency here is always ISO 4217.
CREATE TABLE IF NOT EXISTS prices (
    listing_id UUID    NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
    date       DATE    NOT NULL,
    open       DOUBLE PRECISION,
    high       DOUBLE PRECISION,
    low        DOUBLE PRECISION,
    close      DOUBLE PRECISION NOT NULL,
    volume     BIGINT,
    currency   CHAR(3) NOT NULL,
    adjusted   BOOLEAN NOT NULL DEFAULT TRUE,  -- auto_adjust=True at fetch time
    PRIMARY KEY (listing_id, date)
);

CREATE INDEX IF NOT EXISTS idx_prices_date ON prices (date);
CREATE INDEX IF NOT EXISTS idx_prices_currency ON prices (currency);


-- ══ FX ═════════════════════════════════════════════════════════════════════
-- Point-in-time reference rates so any price can be expressed in a common
-- currency. ECB publishes EUR-based daily reference rates (keyless), so rows
-- are populated with base_currency='EUR' and inverted/crossed in the
-- repository layer rather than stored redundantly.
--
-- FX holidays do not align with equity calendars, so conversion MUST join on
-- the most recent rate on or before the price date, never on an exact date
-- match — an equi-join silently drops bars.

CREATE TABLE IF NOT EXISTS fx_rates (
    date           DATE    NOT NULL,
    base_currency  CHAR(3) NOT NULL,
    quote_currency CHAR(3) NOT NULL,
    rate           DOUBLE PRECISION NOT NULL CHECK (rate > 0),  -- 1 base = rate quote
    source         TEXT    NOT NULL,   -- 'ecb'
    PRIMARY KEY (date, base_currency, quote_currency),
    CONSTRAINT fx_distinct_currencies CHECK (base_currency <> quote_currency)
);

CREATE INDEX IF NOT EXISTS idx_fx_lookup ON fx_rates (base_currency, quote_currency, date DESC);


-- ══ Corporate actions ══════════════════════════════════════════════════════
-- Splits and structural events. Required to fix audit A1.4: today prices are
-- appended from last_date+1 with auto_adjust=True, so any split glues
-- pre-adjustment history to post-adjustment rows and fabricates a return on
-- the seam. Detecting an action here triggers a full re-fetch of that listing.
--
-- Cash dividends keep their own table (separate fetch path, separate source);
-- this table covers splits, redenominations and structural events.

CREATE TABLE IF NOT EXISTS corporate_actions (
    action_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id     UUID NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
    action_type    TEXT NOT NULL
        CHECK (action_type IN ('split', 'reverse_split', 'spinoff', 'merger',
                               'ticker_change', 'redenomination', 'delisting')),
    effective_date DATE NOT NULL,
    ratio          DOUBLE PRECISION CHECK (ratio IS NULL OR ratio > 0),  -- 2.0 = 2:1 split
    from_value     TEXT,              -- old ticker / old currency
    to_value       TEXT,              -- new ticker / new currency
    source         TEXT NOT NULL,     -- 'yfinance' | 'manual'
    notes          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (listing_id, action_type, effective_date)
);

CREATE INDEX IF NOT EXISTS idx_corp_actions_date ON corporate_actions (effective_date DESC);
CREATE INDEX IF NOT EXISTS idx_corp_actions_listing ON corporate_actions (listing_id, effective_date DESC);


CREATE TABLE IF NOT EXISTS dividends (
    listing_id UUID    NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
    ex_date    DATE    NOT NULL,
    amount     DOUBLE PRECISION NOT NULL,
    currency   CHAR(3) NOT NULL,
    PRIMARY KEY (listing_id, ex_date)
);


CREATE TABLE IF NOT EXISTS short_interest (
    listing_id     UUID NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
    settlement_date DATE NOT NULL,
    short_volume   BIGINT,
    total_volume   BIGINT,
    days_to_cover  DOUBLE PRECISION,
    PRIMARY KEY (listing_id, settlement_date)
);


-- Derived, recomputed from prices. Truncate-and-rebuild, never appended.
CREATE TABLE IF NOT EXISTS volatility (
    listing_id UUID NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
    date       DATE NOT NULL,
    rv_20d     DOUBLE PRECISION,
    rv_60d     DOUBLE PRECISION,
    PRIMARY KEY (listing_id, date)
);


-- Macro is not company-scoped.
CREATE TABLE IF NOT EXISTS macro_series (
    series_id   TEXT PRIMARY KEY,         -- 'DGS10', 'VIXCLS', ECB keys
    source      TEXT NOT NULL,            -- 'fred' | 'ecb'
    title       TEXT,
    unit        TEXT,
    frequency   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS macro_observations (
    series_id TEXT NOT NULL REFERENCES macro_series(series_id) ON DELETE CASCADE,
    date      DATE NOT NULL,
    value     DOUBLE PRECISION,
    PRIMARY KEY (series_id, date)
);


-- ══ News ═══════════════════════════════════════════════════════════════════
-- Canonical store. The NDJSON under data/raw/news/ stays as an immutable
-- archive but is no longer the read path.

CREATE TABLE IF NOT EXISTS articles (
    article_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    url_hash     TEXT        NOT NULL,
    content_hash TEXT        NOT NULL,
    url          TEXT        NOT NULL,
    source       TEXT        NOT NULL,    -- 'google-news' | 'yahoo-finance' | 'sec:8-K'
    title        TEXT,
    summary      TEXT,
    published_at TIMESTAMPTZ NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Content dedup stays global: the same story is one row.
CREATE UNIQUE INDEX IF NOT EXISTS uq_articles_url_hash     ON articles (url_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_articles_content_hash ON articles (content_hash);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_title_trgm   ON articles USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_articles_summary_trgm ON articles USING gin (summary gin_trgm_ops);


-- Attribution and processing status are PER COMPANY, so one macro story can be
-- processed for 400 companies instead of being dropped for 399 (audit A1.2).
CREATE TABLE IF NOT EXISTS article_companies (
    article_id UUID NOT NULL REFERENCES articles(article_id) ON DELETE CASCADE,
    company_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    pre_score  DOUBLE PRECISION NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processed','redundant','error')),
    error      TEXT,                      -- last failure reason when status='error'
    attempts   INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (article_id, company_id)
);

-- Drives the retry sweep: errored rows are reprocessable, not dedup-skipped.
CREATE INDEX IF NOT EXISTS idx_article_companies_status ON article_companies (status, updated_at);


-- ══ Facts ══════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS company_facts (
    fact_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id    UUID        NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    fact_type     TEXT        NOT NULL,   -- free-form, LLM-assigned
    statement     TEXT        NOT NULL,
    confidence    DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    valid_from    DATE        NOT NULL,
    valid_until   DATE,                   -- NULL = current
    supersedes_id UUID        REFERENCES company_facts(fact_id),
    source_article UUID       REFERENCES articles(article_id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fact_window_valid CHECK (valid_until IS NULL OR valid_until >= valid_from)
);

-- The point-in-time access path. Every fact read goes through this shape.
CREATE INDEX IF NOT EXISTS idx_facts_pit ON company_facts (company_id, valid_from, valid_until);
CREATE INDEX IF NOT EXISTS idx_facts_type ON company_facts (fact_type);


CREATE TABLE IF NOT EXISTS fact_embeddings (
    fact_id    UUID PRIMARY KEY REFERENCES company_facts(fact_id) ON DELETE CASCADE,
    model      TEXT NOT NULL,             -- 'hash-ngram' now; API model later
    embedding  vector(1536) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fact_embeddings_hnsw
    ON fact_embeddings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_fact_embeddings_model ON fact_embeddings (model);


CREATE TABLE IF NOT EXISTS aggregate_facts (
    aggregate_id  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    dimension     TEXT        NOT NULL,   -- 'sector' | 'geography'
    dimension_key TEXT        NOT NULL,
    statement     TEXT        NOT NULL,
    fact_type     TEXT        NOT NULL,
    confidence    DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    valid_from    DATE        NOT NULL,
    valid_until   DATE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_aggregate_pit ON aggregate_facts (dimension, dimension_key, valid_from, valid_until);

-- Evidence trail: which company facts backed an aggregate.
CREATE TABLE IF NOT EXISTS aggregate_fact_evidence (
    aggregate_id UUID NOT NULL REFERENCES aggregate_facts(aggregate_id) ON DELETE CASCADE,
    fact_id      UUID NOT NULL REFERENCES company_facts(fact_id) ON DELETE CASCADE,
    PRIMARY KEY (aggregate_id, fact_id)
);


-- ══ Company profiles ═══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS company_profiles (
    profile_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id       UUID        NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    description      TEXT        NOT NULL,
    products_services TEXT[]     NOT NULL DEFAULT '{}',
    website_url      TEXT,
    sources          JSONB,
    generated_by     TEXT        NOT NULL,
    valid_from       DATE        NOT NULL,
    valid_until      DATE,
    supersedes_id    UUID        REFERENCES company_profiles(profile_id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_profiles_pit ON company_profiles (company_id, valid_from, valid_until);


-- ══ Market views ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS market_views (
    view_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    as_of       DATE        NOT NULL,     -- market date the view refers to
    scope       TEXT        NOT NULL
        CHECK (scope IN ('market','ticker','theme')),
    company_id  UUID        REFERENCES companies(company_id) ON DELETE SET NULL,
    theme       TEXT,
    view_text   TEXT        NOT NULL,     -- immutable, never edited in place
    written_by  TEXT        NOT NULL,     -- model id
    data_cutoff DATE        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT view_scope_target CHECK (
        (scope = 'ticker' AND company_id IS NOT NULL) OR
        (scope = 'theme'  AND theme IS NOT NULL) OR
        (scope = 'market')
    )
);

CREATE INDEX IF NOT EXISTS idx_views_asof  ON market_views (as_of DESC);
CREATE INDEX IF NOT EXISTS idx_views_scope ON market_views (scope, as_of DESC);


-- Derived, never authoritative. Versioned by schema_version (audit A1.9).
CREATE TABLE IF NOT EXISTS market_view_structures (
    view_id        UUID    NOT NULL REFERENCES market_views(view_id) ON DELETE CASCADE,
    schema_version INTEGER NOT NULL,
    structure      JSONB   NOT NULL,
    extracted_by   TEXT    NOT NULL,      -- actual model id, not a hardcoded string
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (view_id, schema_version)
);


-- ══ Filings ════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS filings (
    filing_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  UUID        REFERENCES companies(company_id) ON DELETE SET NULL,
    accession   TEXT        NOT NULL UNIQUE,
    form_type   TEXT        NOT NULL,     -- '8-K', '10-K', '4', '13F-HR'
    filed_at    DATE        NOT NULL,
    period      DATE,
    url         TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'fetched',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_filings_company_date ON filings (company_id, filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_filings_form         ON filings (form_type, filed_at DESC);

CREATE TABLE IF NOT EXISTS filing_text (
    filing_id UUID PRIMARY KEY REFERENCES filings(filing_id) ON DELETE CASCADE,
    section   TEXT,
    body      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS insider_transactions (
    transaction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id      UUID NOT NULL REFERENCES filings(filing_id) ON DELETE CASCADE,
    company_id     UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    insider_name   TEXT NOT NULL,
    insider_title  TEXT,
    transaction_date DATE NOT NULL,
    transaction_code TEXT,
    shares         DOUBLE PRECISION,
    price          DOUBLE PRECISION,
    is_derivative  BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_insider_tx ON insider_transactions (company_id, transaction_date DESC);

CREATE TABLE IF NOT EXISTS insider_holdings (
    holding_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id    UUID NOT NULL REFERENCES filings(filing_id) ON DELETE CASCADE,
    company_id   UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    insider_name TEXT NOT NULL,
    as_of        DATE NOT NULL,
    shares       DOUBLE PRECISION,
    is_derivative BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_insider_holdings ON insider_holdings (company_id, as_of DESC);

CREATE TABLE IF NOT EXISTS form13f_holdings (
    holding_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id     UUID NOT NULL REFERENCES filings(filing_id) ON DELETE CASCADE,
    manager_cik   TEXT NOT NULL,
    manager_name  TEXT,
    company_id    UUID REFERENCES companies(company_id) ON DELETE SET NULL,
    cusip         TEXT NOT NULL,
    report_period DATE NOT NULL,
    shares        DOUBLE PRECISION,
    value_usd     DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_13f_period  ON form13f_holdings (report_period DESC);
CREATE INDEX IF NOT EXISTS idx_13f_company ON form13f_holdings (company_id, report_period DESC);


-- European (ESEF/UKSEF) annual reports from filings.xbrl.org. A separate table
-- from `filings` because the provider, the keys, and the entity identity all
-- differ: EDGAR is CIK/accession-keyed, ESEF is LEI/fxo_id-keyed.
--
-- `lei` is kept as the raw provider value even though company_identifiers
-- holds LEIs, because a filing may reference an issuer outside our universe —
-- in which case company_id is NULL but the filing is still worth storing.
CREATE TABLE IF NOT EXISTS esef_filings (
    esef_filing_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    fxo_id           TEXT        NOT NULL,   -- filings.xbrl.org index id
    lei              TEXT        NOT NULL,   -- 20-char issuer LEI, as filed
    company_id       UUID        REFERENCES companies(company_id) ON DELETE SET NULL,
    entity_name      TEXT,
    country          TEXT        NOT NULL,
    filing_system    TEXT        NOT NULL DEFAULT 'ESEF'
        CHECK (filing_system IN ('ESEF', 'UKSEF')),
    provider         TEXT        NOT NULL DEFAULT 'filings.xbrl.org',
    filed_at         DATE        NOT NULL,   -- index date (market-visibility proxy)
    period_end       DATE,
    primary_document TEXT,
    package_url      TEXT        NOT NULL,
    xhtml_url        TEXT,
    json_url         TEXT,
    text_path        TEXT,                   -- path under data/raw/filings-eu/
    text_hash        TEXT,                   -- sha256(package)
    size_bytes       INTEGER,
    status           TEXT        NOT NULL DEFAULT 'fetched',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT lei_is_20_chars CHECK (char_length(lei) = 20),
    UNIQUE (fxo_id, lei)
);

CREATE INDEX IF NOT EXISTS idx_esef_lei_date     ON esef_filings (lei, filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_esef_company_date ON esef_filings (company_id, filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_esef_country_date ON esef_filings (country, filed_at DESC);


-- ══ Audit ══════════════════════════════════════════════════════════════════

-- Written at START and updated at end, so a killed run leaves a trace (BUG 7).
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_date    DATE        NOT NULL,
    job_type    TEXT        NOT NULL,     -- 'news' | 'fetch' | 'views' | 'filings' | 'profiles'
    status      TEXT        NOT NULL DEFAULT 'started'
        CHECK (status IN ('started','succeeded','failed','aborted')),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    counters    JSONB       NOT NULL DEFAULT '{}',  -- per-job counts, no schema churn
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_recent ON agent_runs (job_type, started_at DESC);


-- Per-company resume checkpoints so a killed run restarts where it stopped.
CREATE TABLE IF NOT EXISTS ingest_checkpoints (
    run_date   DATE NOT NULL,
    company_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    stage      TEXT NOT NULL,             -- 'profile' | 'fetch' | 'extract'
    status     TEXT NOT NULL,             -- 'done' | 'failed'
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_date, company_id, stage)
);

CREATE TABLE IF NOT EXISTS fetch_checkpoints (
    source     TEXT NOT NULL,             -- 'prices' | 'macro' | 'dividends' | ...
    key        TEXT NOT NULL,             -- symbol or series id
    last_date  DATE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, key)
);
