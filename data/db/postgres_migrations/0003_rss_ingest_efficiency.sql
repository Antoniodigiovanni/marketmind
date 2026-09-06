ALTER TABLE articles
    ADD COLUMN IF NOT EXISTS publisher TEXT;

ALTER TABLE article_companies
    DROP CONSTRAINT IF EXISTS article_companies_status_check;
ALTER TABLE article_companies
    ADD CONSTRAINT article_companies_status_check
    CHECK (status IN ('pending','processed','redundant','filtered','error'));

ALTER TABLE ingest_checkpoints
    ADD COLUMN IF NOT EXISTS last_published_at TIMESTAMPTZ;
