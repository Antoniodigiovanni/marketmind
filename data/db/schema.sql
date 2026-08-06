-- marketmind DuckDB schema
-- All timestamps are UTC. Run once at DB initialisation.

-- ── Articles ──────────────────────────────────────────────────────────────────
-- Deduplication registry. Raw article content is not stored here — only metadata
-- and fingerprints. Raw text lives in data/raw/news/ as line-delimited JSON.

CREATE TABLE IF NOT EXISTS articles (
    id               VARCHAR PRIMARY KEY,   -- uuid7
    url_hash         VARCHAR NOT NULL,      -- sha256(url), dedup key
    content_hash     VARCHAR NOT NULL,      -- sha256(normalized_text), dedup key
    url              VARCHAR NOT NULL,
    source           VARCHAR NOT NULL,      -- e.g. 'google-news', 'yahoo-finance', 'sec:10-K'
    ticker           VARCHAR NOT NULL,      -- primary ticker this article was fetched for
    published_at     TIMESTAMPTZ NOT NULL,
    fetched_at       TIMESTAMPTZ NOT NULL,
    title            VARCHAR,
    pre_score        DOUBLE,               -- source_credibility * recency_decay, pre-LLM
    status           VARCHAR NOT NULL      -- 'pending' | 'processed' | 'redundant' | 'error'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_url_hash     ON articles (url_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_content_hash ON articles (content_hash);
CREATE        INDEX IF NOT EXISTS idx_articles_ticker_date  ON articles (ticker, published_at);


-- ── SEC filings ─────────────────────────────────────────────────────────────
-- One row per EDGAR filing for covered form types. filed_at is the
-- point-in-time anchor (when the market could see it); period_of_report is
-- what the filing refers to.

CREATE TABLE IF NOT EXISTS filings (
    id               VARCHAR PRIMARY KEY,   -- uuid4 hex
    accession_number VARCHAR NOT NULL,      -- EDGAR accession, no dashes
    cik              VARCHAR NOT NULL,      -- 10-digit issuer CIK
    ticker           VARCHAR,               -- resolved at ingest
    form_type        VARCHAR NOT NULL,      -- '10-K', '10-Q', '8-K', '3', '4', '5', '13F-HR', ...
    filed_at         DATE NOT NULL,         -- filing date (point-in-time anchor)
    period_of_report DATE,                  -- report period / event date
    primary_document VARCHAR,               -- archive document filename
    url              VARCHAR NOT NULL,
    url_hash         VARCHAR NOT NULL,      -- sha256(url)
    text_path        VARCHAR,               -- path under data/raw/filings/
    text_hash        VARCHAR,               -- sha256(normalized text) / raw XML
    size_bytes       INTEGER,
    status           VARCHAR NOT NULL DEFAULT 'fetched',
    article_id       VARCHAR REFERENCES articles(id),  -- set when queued for fact extraction
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (accession_number, cik)
);

CREATE INDEX IF NOT EXISTS idx_filings_ticker_date ON filings (ticker, filed_at);
CREATE INDEX IF NOT EXISTS idx_filings_form_type   ON filings (form_type);
CREATE INDEX IF NOT EXISTS idx_filings_status      ON filings (status);


-- ── Filing full text (DuckDB FTS input) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS filing_text (
    filing_id  VARCHAR PRIMARY KEY REFERENCES filings(id),
    text       VARCHAR NOT NULL,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── Insider forms 3/4/5 ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS insider_transactions (
    id                    VARCHAR PRIMARY KEY,
    filing_id             VARCHAR NOT NULL REFERENCES filings(id),
    ticker                VARCHAR NOT NULL,
    owner_name            VARCHAR NOT NULL,
    owner_cik             VARCHAR,
    owner_title           VARCHAR,
    security_title        VARCHAR NOT NULL,
    transaction_date      DATE,
    deemed_execution_date DATE,
    transaction_code      VARCHAR,           -- 'P' | 'S' | 'A' | 'D' | ...
    shares_transacted     DOUBLE,
    price_per_share       DOUBLE,
    shares_held_after     DOUBLE,
    direct_or_indirect    VARCHAR,           -- 'D' | 'I'
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_insider_tx_ticker_date
    ON insider_transactions (ticker, transaction_date);

CREATE TABLE IF NOT EXISTS insider_holdings (
    id                 VARCHAR PRIMARY KEY,
    filing_id          VARCHAR NOT NULL REFERENCES filings(id),
    ticker             VARCHAR NOT NULL,
    owner_name         VARCHAR NOT NULL,
    owner_cik          VARCHAR,
    owner_title        VARCHAR,
    security_title     VARCHAR NOT NULL,
    ref_date           DATE,                 -- transaction / report date
    shares_held_after  DOUBLE,
    direct_or_indirect VARCHAR,
    form_type          VARCHAR NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_insider_holdings_ticker_date
    ON insider_holdings (ticker, ref_date);


-- ── 13F filings (metadata) and holdings (Parquet-backed view) ───────────────
CREATE TABLE IF NOT EXISTS form13f_filings (
    id            VARCHAR PRIMARY KEY,
    filing_id     VARCHAR NOT NULL REFERENCES filings(id),
    filer_cik     VARCHAR NOT NULL,
    filer_name    VARCHAR,
    report_period DATE NOT NULL,             -- quarter end
    total_value   DOUBLE,                    -- USD
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_13f_filings_period ON form13f_filings (report_period);

-- Holdings live in data/raw/13f/{year}/Q{n}.parquet. There is deliberately no
-- view here: DuckDB evaluates read_parquet globs eagerly at CREATE VIEW time
-- and errors when no partitions exist yet. Tools read the guarded glob
-- directly (see server.mcp.get_13f_holdings).


-- ── Backfill checkpoints ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fetch_checkpoints (
    source       VARCHAR NOT NULL,
    target_key   VARCHAR NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, target_key)
);


-- ── Ticker facts ──────────────────────────────────────────────────────────────
-- Versioned knowledge base. Facts are never mutated — only superseded.
-- Always query with a point-in-time filter to avoid look-ahead bias:
--   WHERE valid_from <= :as_of AND (valid_until IS NULL OR valid_until > :as_of)

CREATE TABLE IF NOT EXISTS ticker_facts (
    id               VARCHAR PRIMARY KEY,   -- uuid7
    ticker           VARCHAR NOT NULL,
    fact_type        VARCHAR NOT NULL,      -- free-form: 'guidance', 'management', 'risk', ...
    statement        VARCHAR NOT NULL,      -- natural language fact
    confidence       DOUBLE  NOT NULL,      -- 0.0–1.0, hybrid: pre_score adjusted by LLM
    valid_from       DATE    NOT NULL,      -- date article was published
    valid_until      DATE,                  -- NULL = still current; set when superseded
    source_article   VARCHAR NOT NULL REFERENCES articles(id),
    embedding_id     VARCHAR,               -- ChromaDB vector ID
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_facts_ticker        ON ticker_facts (ticker);
CREATE INDEX IF NOT EXISTS idx_facts_ticker_valid  ON ticker_facts (ticker, valid_from, valid_until);
CREATE INDEX IF NOT EXISTS idx_facts_type          ON ticker_facts (fact_type);


-- ── Agent runs ────────────────────────────────────────────────────────────────
-- Audit log for every ingest pipeline execution.

CREATE TABLE IF NOT EXISTS agent_runs (
    id               VARCHAR PRIMARY KEY,
    run_date         DATE        NOT NULL,
    job_type         VARCHAR     NOT NULL DEFAULT 'news',   -- 'news' | 'filings'
    started_at       TIMESTAMPTZ NOT NULL,
    finished_at      TIMESTAMPTZ,
    articles_fetched INTEGER,
    articles_skipped INTEGER,    -- Level-1 dedup hits
    articles_redundant INTEGER,  -- Level-2 (LLM) redundant
    facts_written    INTEGER,
    facts_superseded INTEGER,
    filings_fetched  INTEGER,
    filings_parsed   INTEGER,
    filings_skipped  INTEGER,
    insider_rows     INTEGER,
    form13f_rows     INTEGER,
    error            VARCHAR     -- NULL if run succeeded
);


-- ── Market views ─────────────────────────────────────────────────────────────
-- Free-text market snapshots written by the agent harness via MCP.
-- Immutable once written. Structured extraction lives in market_view_structures.

CREATE TABLE IF NOT EXISTS market_views (
    id           VARCHAR PRIMARY KEY,   -- uuid7
    as_of        DATE        NOT NULL,  -- market date the view refers to
    scope        VARCHAR     NOT NULL,  -- 'market' | 'ticker' | 'theme'
    ticker       VARCHAR,               -- set when scope = 'ticker'
    theme        VARCHAR,               -- set when scope = 'theme'
    view_text    VARCHAR     NOT NULL,  -- free text, never edited in place
    model        VARCHAR     NOT NULL,  -- harness model that produced the view
    data_cutoff  TIMESTAMPTZ,           -- newest data timestamp the view used
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_views_as_of   ON market_views (as_of);
CREATE INDEX IF NOT EXISTS idx_views_ticker  ON market_views (ticker);
CREATE INDEX IF NOT EXISTS idx_views_scope   ON market_views (scope);


-- ── Market view structures ───────────────────────────────────────────────────
-- Derived structured data extracted from free-text views. Never replaces the
-- source text — free text always remains available in market_views.

CREATE TABLE IF NOT EXISTS market_view_structures (
    view_id         VARCHAR PRIMARY KEY REFERENCES market_views(id),
    payload         JSON        NOT NULL,  -- structured extraction, schema_version-driven
    schema_version  VARCHAR     NOT NULL,  -- e.g. 'v1'
    extracted_by    VARCHAR     NOT NULL,  -- extraction model
    extracted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── Symbol map ───────────────────────────────────────────────────────────────
-- Persistent provider-symbol resolution keyed by stable security identifiers
-- (ISIN / CUSIP / ticker). Built by data/universe/resolve.py and re-resolved
-- periodically so symbol changes are absorbed without code changes.

CREATE TABLE IF NOT EXISTS symbol_map (
    id_type          VARCHAR     NOT NULL,  -- 'ISIN' | 'CUSIP' | 'TICKER'
    id_value         VARCHAR     NOT NULL,  -- e.g. 'US0378331005', 'AAPL'
    provider         VARCHAR     NOT NULL DEFAULT 'yahoo',
    symbol           VARCHAR     NOT NULL,  -- resolved provider symbol
    name             VARCHAR,               -- resolved display name, if known
    resolved_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_validated_at TIMESTAMPTZ,
    status           VARCHAR     NOT NULL DEFAULT 'valid',  -- 'valid'|'review'|'unresolved'
    PRIMARY KEY (id_type, id_value, provider)
);
