
CREATE TABLE research_candidates (
	id UUID NOT NULL,
	run_id UUID NOT NULL,
	attempt_id UUID NOT NULL,
	generation INTEGER NOT NULL,
	candidate_key VARCHAR(160) NOT NULL,
	parent_id UUID,
	kind VARCHAR(20) NOT NULL,
	iteration INTEGER NOT NULL,
	repair_index INTEGER NOT NULL,
	code TEXT NOT NULL,
	code_sha256 VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_candidate_run_key UNIQUE (run_id, candidate_key),
	CHECK (kind IN ('seed','generated','repair')),
	CHECK (generation > 0 AND iteration >= 0 AND repair_index >= 0),
	FOREIGN KEY(run_id) REFERENCES research_runs (id),
	FOREIGN KEY(attempt_id) REFERENCES execution_attempts (id),
	FOREIGN KEY(parent_id) REFERENCES research_candidates (id)
)

;
CREATE INDEX ix_research_candidates_run_id ON research_candidates (run_id);

CREATE TABLE research_evaluations (
	id UUID NOT NULL,
	run_id UUID NOT NULL,
	attempt_id UUID NOT NULL,
	generation INTEGER NOT NULL,
	candidate_id UUID,
	candidate_key VARCHAR(160) NOT NULL,
	code_sha256 VARCHAR(64) NOT NULL,
	split VARCHAR(20) NOT NULL,
	"window" JSONB NOT NULL,
	window_sha256 VARCHAR(64) NOT NULL,
	image_digest VARCHAR(255) NOT NULL,
	sandbox_id VARCHAR(255) NOT NULL,
	sandbox_stopped BOOLEAN NOT NULL,
	status VARCHAR(30) NOT NULL,
	payload JSONB,
	payload_sha256 VARCHAR(64),
	error TEXT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CHECK (split IN ('train','validation')),
	CHECK (status IN ('registered','succeeded','failed','cancelled','interrupted')),
	CHECK (generation > 0),
	FOREIGN KEY(run_id) REFERENCES research_runs (id),
	FOREIGN KEY(attempt_id) REFERENCES execution_attempts (id),
	FOREIGN KEY(candidate_id) REFERENCES research_candidates (id),
	UNIQUE (sandbox_id)
)

;
CREATE INDEX ix_research_evaluations_attempt_id ON research_evaluations (attempt_id);
CREATE INDEX ix_research_evaluations_run_id ON research_evaluations (run_id);

CREATE TABLE research_model_calls (
	id UUID NOT NULL,
	run_id UUID NOT NULL,
	attempt_id UUID NOT NULL,
	generation INTEGER NOT NULL,
	logical_call_id VARCHAR(160) NOT NULL,
	provider VARCHAR(40) NOT NULL,
	model VARCHAR(160) NOT NULL,
	prompt_version VARCHAR(160) NOT NULL,
	status VARCHAR(30) NOT NULL,
	adopted BOOLEAN NOT NULL,
	request_sha256 VARCHAR(64) NOT NULL,
	response_sha256 VARCHAR(64),
	response_artifact JSONB,
	usage JSONB NOT NULL,
	latency_seconds FLOAT NOT NULL,
	error_category VARCHAR(80),
	payload JSONB NOT NULL,
	payload_sha256 VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_model_call_run_logical UNIQUE (run_id, logical_call_id),
	FOREIGN KEY(run_id) REFERENCES research_runs (id),
	FOREIGN KEY(attempt_id) REFERENCES execution_attempts (id)
)

;
CREATE INDEX ix_research_model_calls_run_id ON research_model_calls (run_id);

CREATE TABLE research_checkpoints (
	id UUID NOT NULL,
	run_id UUID NOT NULL,
	attempt_id UUID NOT NULL,
	generation INTEGER NOT NULL,
	sequence INTEGER NOT NULL,
	schema_version INTEGER NOT NULL,
	manifest_storage_key VARCHAR(500) NOT NULL,
	manifest_sha256 VARCHAR(64) NOT NULL,
	input_digest VARCHAR(64) NOT NULL,
	config_sha256 VARCHAR(64) NOT NULL,
	summary JSONB NOT NULL,
	next_stage VARCHAR(80) NOT NULL,
	iteration INTEGER NOT NULL,
	complete BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_checkpoint_run_sequence UNIQUE (run_id, sequence),
	CONSTRAINT uq_checkpoint_attempt_manifest UNIQUE (attempt_id, manifest_sha256),
	CHECK (complete = true AND generation > 0 AND sequence > 0),
	FOREIGN KEY(run_id) REFERENCES research_runs (id),
	FOREIGN KEY(attempt_id) REFERENCES execution_attempts (id)
)

;
CREATE INDEX ix_research_checkpoints_run_id ON research_checkpoints (run_id);
