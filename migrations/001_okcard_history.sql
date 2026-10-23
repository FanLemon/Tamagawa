-- Add OKCard storage only; existing exchange-rate tables are unchanged.

BEGIN;

CREATE TABLE IF NOT EXISTS okcard_purchases (
	id SERIAL NOT NULL,
	account_key VARCHAR NOT NULL,
	source_purchase_id VARCHAR NOT NULL,
	purchase_date TIMESTAMP WITH TIME ZONE NOT NULL,
	store_name VARCHAR NOT NULL,
	purchase_price NUMERIC(18, 4) NOT NULL,
	raw_payload JSONB NOT NULL,
	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_okcard_purchase_source UNIQUE (account_key, source_purchase_id)
);

CREATE INDEX IF NOT EXISTS ix_okcard_purchase_month ON okcard_purchases (account_key, purchase_date);

CREATE TABLE IF NOT EXISTS okcard_sync_state (
	account_key VARCHAR NOT NULL,
	identity_field VARCHAR NOT NULL,
	last_attempt_at TIMESTAMP WITH TIME ZONE,
	last_success_at TIMESTAMP WITH TIME ZONE,
	last_error VARCHAR,
	PRIMARY KEY (account_key)
);

COMMIT;
