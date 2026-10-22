
CREATE TABLE instruments (
	id UUID NOT NULL, 
	asset_type VARCHAR(30) NOT NULL, 
	identity_status VARCHAR(30) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE provider_budgets (
	key VARCHAR(255) NOT NULL, 
	next_allowed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (key)
)

;

CREATE TABLE listings (
	id UUID NOT NULL, 
	instrument_id UUID NOT NULL, 
	symbol VARCHAR(80) NOT NULL, 
	mic VARCHAR(4) NOT NULL, 
	currency VARCHAR(16) NOT NULL, 
	timezone VARCHAR(80) NOT NULL, 
	calendar VARCHAR(80) NOT NULL, 
	metadata_json JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(instrument_id) REFERENCES instruments (id)
)

;

CREATE TABLE universe_versions (
	id UUID NOT NULL, 
	owner_id UUID NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	state VARCHAR(40) NOT NULL, 
	as_of TIMESTAMP WITH TIME ZONE NOT NULL, 
	members JSONB NOT NULL, 
	evidence JSONB NOT NULL, 
	content_sha256 VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (state IN ('user_fixed','current_snapshot','historical_membership','point_in_time_verified')), 
	FOREIGN KEY(owner_id) REFERENCES users (id)
)

;
CREATE INDEX ix_universe_versions_owner_id ON universe_versions (owner_id);

CREATE TABLE data_imports (
	id UUID NOT NULL, 
	owner_id UUID NOT NULL, 
	kind VARCHAR(20) NOT NULL, 
	provider VARCHAR(30) NOT NULL, 
	idempotency_key VARCHAR(128) NOT NULL, 
	request_sha256 VARCHAR(64) NOT NULL, 
	request JSONB NOT NULL, 
	listing_ids JSONB NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	generation INTEGER NOT NULL, 
	lease_token UUID, 
	lease_expires_at TIMESTAMP WITH TIME ZONE, 
	progress JSONB NOT NULL, 
	warnings JSONB NOT NULL, 
	error TEXT, 
	cancel_requested BOOLEAN NOT NULL, 
	dataset_version_id UUID, 
	parent_version_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_data_import_owner_key UNIQUE (owner_id, idempotency_key), 
	CHECK (status IN ('requested','fetching','normalizing','validating','ready','partial','failed','cancelled')), 
	CHECK (generation > 0), 
	FOREIGN KEY(owner_id) REFERENCES users (id), 
	FOREIGN KEY(dataset_version_id) REFERENCES dataset_versions (id), 
	FOREIGN KEY(parent_version_id) REFERENCES dataset_versions (id)
)

;
CREATE INDEX ix_data_imports_owner_id ON data_imports (owner_id);
CREATE INDEX ix_data_imports_status ON data_imports (status);

CREATE TABLE owner_listings (
	owner_id UUID NOT NULL, 
	listing_id UUID NOT NULL, 
	PRIMARY KEY (owner_id, listing_id), 
	FOREIGN KEY(owner_id) REFERENCES users (id), 
	FOREIGN KEY(listing_id) REFERENCES listings (id)
)

;

CREATE TABLE vendor_mappings (
	id UUID NOT NULL, 
	listing_id UUID NOT NULL, 
	vendor VARCHAR(80) NOT NULL, 
	symbol VARCHAR(80) NOT NULL, 
	valid_from DATE, 
	valid_to DATE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_listing_vendor_period UNIQUE (listing_id, vendor, symbol, valid_from, valid_to), 
	FOREIGN KEY(listing_id) REFERENCES listings (id)
)

;
CREATE INDEX ix_vendor_mappings_listing_id ON vendor_mappings (listing_id);

CREATE TABLE data_blocks (
	id UUID NOT NULL, 
	import_id UUID NOT NULL, 
	symbol VARCHAR(80) NOT NULL, 
	listing_id UUID, 
	start_date DATE NOT NULL, 
	end_date DATE NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	attempts INTEGER NOT NULL, 
	storage_key VARCHAR(500), 
	content_sha256 VARCHAR(64), 
	coverage JSONB NOT NULL, 
	error TEXT, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_data_block_window UNIQUE (import_id, symbol, start_date, end_date), 
	FOREIGN KEY(import_id) REFERENCES data_imports (id), 
	FOREIGN KEY(listing_id) REFERENCES listings (id)
)

;
CREATE INDEX ix_data_blocks_import_id ON data_blocks (import_id);

CREATE TABLE data_outbox (
	id UUID NOT NULL, 
	import_id UUID NOT NULL, 
	generation INTEGER NOT NULL, 
	job_id INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	dispatched_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_data_outbox_generation UNIQUE (import_id, generation), 
	FOREIGN KEY(import_id) REFERENCES data_imports (id)
)

;
