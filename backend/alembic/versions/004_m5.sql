
CREATE TABLE experiments (
	id UUID NOT NULL,
	owner_id UUID NOT NULL,
	name VARCHAR(255) NOT NULL,
	root_experiment_id UUID,
	parent_experiment_id UUID,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(owner_id) REFERENCES users (id),
	FOREIGN KEY(root_experiment_id) REFERENCES experiments (id),
	FOREIGN KEY(parent_experiment_id) REFERENCES experiments (id)
)

;
CREATE INDEX ix_experiments_owner_id ON experiments (owner_id);

CREATE TABLE split_versions (
	id UUID NOT NULL,
	owner_id UUID NOT NULL,
	experiment_id UUID NOT NULL,
	dataset_version_id UUID NOT NULL,
	universe_version_id UUID NOT NULL,
	content_sha256 VARCHAR(64) NOT NULL,
	definition JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(owner_id) REFERENCES users (id),
	FOREIGN KEY(experiment_id) REFERENCES experiments (id),
	FOREIGN KEY(dataset_version_id) REFERENCES dataset_versions (id),
	FOREIGN KEY(universe_version_id) REFERENCES universe_versions (id)
)

;
CREATE INDEX ix_split_versions_experiment_id ON split_versions (experiment_id);
CREATE INDEX ix_split_versions_owner_id ON split_versions (owner_id);

CREATE TABLE final_selections (
	id UUID NOT NULL,
	owner_id UUID NOT NULL,
	experiment_id UUID NOT NULL,
	split_version_id UUID NOT NULL,
	source_run_id UUID NOT NULL,
	source_candidate_id UUID NOT NULL,
	source_checkpoint_id UUID NOT NULL,
	strategy_version_id UUID NOT NULL,
	request_sha256 VARCHAR(64) NOT NULL,
	source_config_sha256 VARCHAR(64) NOT NULL,
	source_code_sha256 VARCHAR(64) NOT NULL,
	source_checkpoint_sha256 VARCHAR(64) NOT NULL,
	selection_evidence JSONB NOT NULL,
	exposure_snapshot JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(owner_id) REFERENCES users (id),
	FOREIGN KEY(experiment_id) REFERENCES experiments (id),
	UNIQUE (split_version_id),
	FOREIGN KEY(split_version_id) REFERENCES split_versions (id),
	FOREIGN KEY(source_run_id) REFERENCES research_runs (id),
	FOREIGN KEY(source_candidate_id) REFERENCES research_candidates (id),
	FOREIGN KEY(source_checkpoint_id) REFERENCES research_checkpoints (id),
	FOREIGN KEY(strategy_version_id) REFERENCES strategy_versions (id)
)

;
CREATE INDEX ix_final_selections_owner_id ON final_selections (owner_id);

CREATE TABLE data_usage_exposures (
	id UUID NOT NULL,
	owner_id UUID NOT NULL,
	experiment_id UUID,
	split_version_id UUID,
	run_id UUID,
	dataset_version_id UUID,
	source_key VARCHAR(255) NOT NULL,
	identities JSONB NOT NULL,
	start_date DATE NOT NULL,
	end_date DATE NOT NULL,
	purpose VARCHAR(80) NOT NULL,
	provenance JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_exposure_owner_source UNIQUE (owner_id, source_key),
	CHECK (start_date < end_date),
	FOREIGN KEY(owner_id) REFERENCES users (id),
	FOREIGN KEY(experiment_id) REFERENCES experiments (id),
	FOREIGN KEY(split_version_id) REFERENCES split_versions (id),
	FOREIGN KEY(run_id) REFERENCES research_runs (id),
	FOREIGN KEY(dataset_version_id) REFERENCES dataset_versions (id)
)

;
CREATE INDEX ix_data_usage_exposures_owner_id ON data_usage_exposures (owner_id);
CREATE INDEX ix_exposure_owner_window ON data_usage_exposures (owner_id, start_date, end_date);

CREATE TABLE strategy_packages (
	id UUID NOT NULL,
	owner_id UUID NOT NULL,
	final_selection_id UUID NOT NULL,
	request_sha256 VARCHAR(64) NOT NULL,
	request JSONB NOT NULL,
	content_sha256 VARCHAR(64) NOT NULL,
	archive_sha256 VARCHAR(64) NOT NULL,
	size_bytes INTEGER NOT NULL,
	storage_key VARCHAR(500) NOT NULL,
	manifest JSONB NOT NULL,
	exposure_snapshot JSONB NOT NULL,
	exposure_as_of TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_package_owner_request UNIQUE (owner_id, request_sha256),
	CHECK (size_bytes > 0),
	FOREIGN KEY(owner_id) REFERENCES users (id),
	FOREIGN KEY(final_selection_id) REFERENCES final_selections (id)
)

;
CREATE INDEX ix_strategy_packages_owner_id ON strategy_packages (owner_id);
