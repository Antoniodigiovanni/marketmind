ALTER TABLE short_interest
    ADD COLUMN IF NOT EXISTS issue_name TEXT,
    ADD COLUMN IF NOT EXISTS market_class TEXT,
    ADD COLUMN IF NOT EXISTS previous_short BIGINT,
    ADD COLUMN IF NOT EXISTS change_pct DOUBLE PRECISION;

ALTER TABLE insider_transactions
    ADD COLUMN IF NOT EXISTS security_title TEXT,
    ADD COLUMN IF NOT EXISTS shares_held_after DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS direct_or_indirect TEXT;

ALTER TABLE insider_holdings
    ADD COLUMN IF NOT EXISTS insider_title TEXT,
    ADD COLUMN IF NOT EXISTS security_title TEXT,
    ADD COLUMN IF NOT EXISTS direct_or_indirect TEXT,
    ADD COLUMN IF NOT EXISTS form_type TEXT;

ALTER TABLE form13f_holdings
    ADD COLUMN IF NOT EXISTS issuer_name TEXT,
    ADD COLUMN IF NOT EXISTS class_title TEXT,
    ADD COLUMN IF NOT EXISTS put_call TEXT;
