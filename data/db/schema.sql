-- alpha-wire DuckDB schema
-- All timestamps are UTC. Run once at DB initialisation.

-- ── Articles ──────────────────────────────────────────────────────────────────
-- Deduplication registry. Raw article content is not stored here — only metadata
-- and fingerprints. Raw text lives in data/raw/news/ as line-delimited JSON.

CREATE TABLE IF NOT EXISTS articles (
    id               VARCHAR PRIMARY KEY,   -- uuid7
    url_hash         VARCHAR NOT NULL,      -- sha256(url), dedup key
    content_hash     VARCHAR NOT NULL,      -- sha256(normalized_text), dedup key
    url              VARCHAR NOT NULL,
    source           VARCHAR NOT NULL,      -- e.g. 'reuters', 'ft', 'newsapi:bbc-news'
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
    started_at       TIMESTAMPTZ NOT NULL,
    finished_at      TIMESTAMPTZ,
    articles_fetched INTEGER,
    articles_skipped INTEGER,    -- Level-1 dedup hits
    articles_redundant INTEGER,  -- Level-2 (LLM) redundant
    facts_written    INTEGER,
    facts_superseded INTEGER,
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
