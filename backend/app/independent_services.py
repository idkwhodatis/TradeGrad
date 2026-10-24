"""Owner-scoped independent leaf admission and immutable manual protocols.

No model/provider path is reachable here. Original sources are readmitted at
creation, dispatch, retry and evidence release; fresh executions never reset
owner-wide exposure or reuse another experiment's split row.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select

from backend.app.comparison_services import _typed_config, load_comparison_side
from backend.app.experiment_schemas import ExperimentCreate, SplitVersionCreate
from backend.app.experiment_services import (
    create_experiment,
    create_split_version,
    get_exposure_snapshot,
    lock_split,
    owned_resource,
    record_exposure,
    split_definition,
)
from backend.app.independent_schemas import (
    DiagnosticReplayCreate,
    IndependentCapability,
)
from backend.app.models import (
    Artifact,
    ControlledArm,
    ControlledExperiment,
    DatasetVersion,
    DataUsageExposure,
    Evaluation,
    ExecutionAttempt,
    Experiment,
    IndependentEvaluation,
    OutboxEvent,
    ResearchRun,
    RunEvent,
    SplitVersion,
    StrategyVersion,
    utcnow,
)
from backend.app.research_services import bounded_json, payload_hash
from backend.app.result_access import record_verified_result_access
from backend.app.services import (
    TERMINAL,
    DomainError,
    advisory_lock,
    append_event,
    fenced_attempt,
    owned_run,
)
from backend.app.storage import read_verified, redact
from backend.app.workspace_policy import quota_limit
from packages.research_trace.contracts import digest
from packages.tradegrad_adapter.config import DateWindow, VersionRef, canonical_bytes
from packages.tradegrad_adapter.independent_config import (
    NATIVE_SCHEMA,
    SCHEMAS,
    IndependentEvaluationConfig,
    PortableIndependentEvaluationConfig,
    contract_without_values,
    parse_independent_config,
)
from packages.tradegrad_adapter.native_contract import NativeStrategyContract

_source_cache = ContextVar("independent_verified_sources", default=None)
MAX_INDEPENDENT_CHAIN = 16


def _integrity():
    return DomainError(
        "independent_integrity",
        "Independent execution differs from its immutable physical source or protocol",
        409,
    )


def independent_config(run):
    try:
        config = parse_independent_config(run.config)
        if config.sha256 != run.request_sha256:
            raise ValueError("Config hash differs")
        return config
    except (ValueError, TypeError, KeyError):
        raise _integrity() from None


def load_owned_runtime_observation(session, result, settings):
    """Optional historical absence is distinct from malformed or missing new evidence."""
    from packages.tradegrad_adapter.runtime_observation import (
        MAX_OBSERVATION_BYTES,
        canonical,
        validate_runtime_observation,
    )

    ref = (result.evaluation.payload or {}).get("runtime_observation")
    artifact = session.scalar(
        select(Artifact).where(
            Artifact.run_id == result.run.id,
            Artifact.attempt_id == result.evaluation.attempt_id,
            Artifact.name
            == f"evaluation-{result.evaluation.id}-runtime-observation.json",
        )
    )
    if artifact is None and ref is None:
        return None, None
    try:
        if (
            artifact is None
            or not isinstance(ref, dict)
            or (str(artifact.id), artifact.sha256, artifact.size_bytes)
            != (ref["artifact_id"], ref["sha256"], ref["size_bytes"])
            or artifact.media_type != "application/json"
            or not 0 < artifact.size_bytes <= MAX_OBSERVATION_BYTES
        ):
            raise ValueError("Observation registration differs")
        raw = read_verified(
            settings.storage_root,
            artifact.storage_key,
            artifact.sha256,
            min(settings.max_artifact_bytes, MAX_OBSERVATION_BYTES),
        )
        value = validate_runtime_observation(
            json.loads(raw),
            expected_identity=result.bundle["identity"],
            expected_runtime_kind="portable"
            if result.bundle["evaluation_mode"] == "portable"
            else "native",
        )
        typed = _typed_config(result.run)
        restored = getattr(typed, "rng_restore", None)
        from packages.tradegrad_adapter.runtime_observation import (
            digest as runtime_digest,
        )

        if value["rng"]["restored_from_sha256"] != (
            runtime_digest(restored) if restored is not None else None
        ):
            raise ValueError("Runtime RNG restoration differs from immutable request")
        window = value["initialization"]["window"]
        if any(
            window.get(key) != result.bundle["window"].get(key)
            for key in ("start", "end")
        ) or (value["runtime_kind"] == "native" and window != result.bundle["window"]):
            raise ValueError(
                "Runtime initialization differs from exact original window/warmup"
            )
        if value["runtime_kind"] == "portable":
            from packages.tradegrad_adapter.portable_windows import (
                _stage_portable_window,
            )

            config = _typed_config(result.run)
            source_config = (
                config.effective_source()
                if config.schema_version in SCHEMAS
                else getattr(config, "source_config", config)
            )
            hashes = result.dataset.manifest.get(
                "files", {}
            ) | result.dataset.manifest.get("artifact_files", {})

            def read_file(name):
                if name not in hashes:
                    raise ValueError("Portable initialization input is unregistered")
                return read_verified(
                    settings.storage_root,
                    f"{result.dataset.storage_key}/{name}",
                    hashes[name],
                    settings.max_artifact_bytes,
                )

            staged = _stage_portable_window(
                source_config,
                result.evaluation.split,
                result.dataset.manifest,
                read_file,
                window=DateWindow(
                    start=result.evaluation.window["start"],
                    end=result.evaluation.window["end"],
                ),
            )
            expected = {
                "policy": "reconstruct_from_original_start.v1",
                "window": {
                    "start": result.evaluation.window["start"],
                    "end": result.evaluation.window["end"],
                    "warmup_start": str(staged.report.staged_start),
                },
            }
            validate_runtime_observation(
                value,
                expected_identity=result.bundle["identity"],
                expected_initialization=expected,
                expected_runtime_kind="portable",
            )
        if len(raw) != artifact.size_bytes or canonical(value) != raw:
            raise ValueError("Observation bytes differ")
        return value, artifact.sha256
    except (ValueError, TypeError, KeyError, AttributeError):
        raise DomainError(
            "runtime_observation_integrity",
            "Runtime initialization/RNG/audit evidence failed immutable verification",
            409,
        ) from None


@dataclass(frozen=True)
class IndependentSource:
    admitted: object
    source_config: object
    manifest: dict | None
    parameters: dict | None
    observation: dict | None
    observation_sha256: str | None
    protocol: dict
    snapshot: dict
    unavailable: tuple[str, ...]

    @property
    def result(self):
        return self.admitted.trace.result


def protocol_for(source_config, manifest, common, observation):
    """Exclude only experimental code identity and parameter VALUES.

    Declaration bounds, schemas, state/lookback/warmup, dependency, fee and fill
    contracts all remain. IDs naming code/parameter blobs cannot encode a factor
    into the supposedly shared protocol through their content hashes.
    """
    raw = source_config.model_dump(mode="json")
    raw.pop("seed_strategy", None)
    raw.pop("research_capture", None)  # observation policy is separately frozen per job
    if "strategy_contract" in raw:
        raw["strategy_contract"] = contract_without_values(raw["strategy_contract"])
    portable = source_config.schema_version.startswith("tradegrad.portable.")
    fixed_manifest = None
    if portable:
        raw.pop("seed_manifest", None)
        raw.pop("seed_parameters", None)
        fixed_manifest = json.loads(canonical_bytes(manifest))
        for key in ("code_sha256", "parameters_sha256", "reference_result_sha256"):
            fixed_manifest.pop(key, None)
    common = json.loads(canonical_bytes(common))
    common["RNG"] = {
        "state": observation["rng"]["state"] if observation else None,
        "status": "recorded_scoped_rng_only" if observation else "not_recorded",
        "uncovered": observation["rng"]["state"]["uncovered"]
        if observation
        else ["historical_rng_unknown"],
    }
    return {
        "schema_version": "tradegrad.control-protocol.v1",
        "fixed_source_config": raw,
        "strategy_contract": fixed_manifest,
        "execution_conditions": common,
        "state_reconstruction": "original_start_fresh_engine_with_original_warmup.v1",
        "excluded_experimental_fields": ["strategy_code", "business_parameter_values"],
        "causal_claim": "not_established_by_backtest_comparison",
    }


def resolve_source(session, owner_id, pin, settings, *, release=True):
    cache = _source_cache.get()
    cache_key = (id(session), str(owner_id), digest(pin))
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    admitted = load_comparison_side(session, pin, owner_id, settings)
    result = admitted.trace.result
    config = _typed_config(result.run)
    source = (
        config.effective_source()
        if config.schema_version in SCHEMAS
        else getattr(config, "source_config", config)
    )
    if result.evaluation.split not in {"train", "validation", "holdout"}:
        raise DomainError(
            "independent_source_unavailable",
            "Source leaf has no supported exact scored window",
            409,
        )
    strategy = owned_resource(
        session, StrategyVersion, result.run.strategy_version_id, owner_id
    )
    portable = source.schema_version.startswith("tradegrad.portable.")
    manifest = (
        json.loads(canonical_bytes(strategy.manifest))
        if strategy.manifest is not None
        else None
    )
    parameters = (
        json.loads(canonical_bytes(strategy.parameters))
        if strategy.parameters is not None
        else None
    )
    if portable:
        # An optimizer candidate's immutable business contract is the seed contract;
        # its only permitted generated-code delta is authenticated separately.
        manifest["code_sha256"] = pin.code_sha256
        manifest["reference_result_sha256"] = None
        from packages.strategy_sdk import hash_json, parse_manifest

        manifest = parse_manifest(canonical_bytes(manifest)).model_dump(mode="json")
        if hash_json(parameters) != source.seed_parameters.sha256:
            raise _integrity()
    else:
        manifest = (
            source.strategy_contract.model_dump(mode="json")
            if source.strategy_contract
            else None
        )
        parameters = (
            source.strategy_contract.parameter_values()
            if source.strategy_contract
            else None
        )
    observation, observation_sha = load_owned_runtime_observation(
        session, result, settings
    )
    unavailable = []
    from packages.tradegrad_adapter.environment import environment_digest
    from packages.tradegrad_adapter.portable_environment import (
        portable_environment_digest,
    )

    expected_environment = (
        portable_environment_digest() if portable else environment_digest()
    )
    if source.environment_digest != expected_environment:
        unavailable.append("source_environment_not_supported_by_current_runtime")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", result.run.research_image_digest or ""):
        unavailable.append("source_engine_image_not_recorded")
    if portable and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", result.run.portable_policy_image_digest or ""
    ):
        unavailable.append("source_policy_image_not_recorded")
    if result.run.split_version_id is None or result.run.experiment_id is None:
        unavailable.append("source_experiment_split_lineage_not_recorded")
    protocol = protocol_for(source, manifest, admitted.input.common_config, observation)
    snapshot = {
        "source": pin.model_dump(mode="json"),
        "source_run_id": str(result.run.id),
        "original_config_sha256": result.run.request_sha256,
        "source_config_sha256": source.sha256,
        "original_window": result.evaluation.window,
        "manifest": manifest,
        "parameters": parameters,
        "runtime_observation_sha256": observation_sha,
        "research_image_digest": result.run.research_image_digest,
        "portable_policy_image_digest": result.run.portable_policy_image_digest,
        "dataset_sha256": result.dataset.content_sha256,
    }
    if release:
        record_verified_result_access(
            session, result, owner_id, purpose="independent_source_read"
        )
    resolved = IndependentSource(
        admitted,
        source,
        manifest,
        parameters,
        observation,
        observation_sha,
        protocol,
        snapshot,
        tuple(unavailable),
    )
    if cache is not None:
        cache[cache_key] = resolved
    return resolved


def source_capability(session, owner_id, pin, settings):
    source = resolve_source(session, owner_id, pin, settings)
    result = source.result
    if result.run.split_version_id is None:
        raise DomainError(
            "independent_source_unavailable",
            "Source has no immutable split lineage",
            409,
        )
    split = owned_resource(session, SplitVersion, result.run.split_version_id, owner_id)
    missing = []
    if source.observation is None:
        missing += [
            "original_rng_not_recorded",
            "original_order_status_audit_not_recorded",
        ]
    elif not (source.observation.get("audit") or {}).get("completed", False):
        missing.append("original_order_status_audit_incomplete")
    if source.admitted.trace.manifest is None:
        missing.append("original_decision_trace_not_collected")
    return IndependentCapability(
        source=pin,
        source_run_id=result.run.id,
        original_config_sha256=result.run.request_sha256,
        original_config=result.run.config,
        evaluation_mode="portable"
        if source.source_config.schema_version.startswith("tradegrad.portable.")
        else "native_akquant",
        can_execute=not source.unavailable,
        unavailable_reasons=list(source.unavailable),
        can_control=not source.unavailable
        and source.observation is not None
        and result.evaluation.split != "holdout",
        control_unavailable_reasons=list(source.unavailable)
        + (["original_rng_not_recorded"] if source.observation is None else [])
        + (
            ["controlled_holdout_forbidden"]
            if result.evaluation.split == "holdout"
            else []
        ),
        parity_prerequisites={
            "status": "potentially_verifiable" if not missing else "not_verifiable",
            "missing": missing,
            "rng_scope": source.observation["rng"]["state"]["scope"]
            if source.observation
            else None,
            "limitations": [
                "No support for OS entropy, unseeded default_rng or strategy self-reseeding",
                "No full engine checkpoint or local counterfactual resume",
            ],
        },
        fixed_conditions=source.protocol,
        protocol_sha256=digest(source.protocol),
        source_code=source.admitted.source.code,
        source_parameters=source.parameters,
        exposure_snapshot=get_exposure_snapshot(session, owner_id, split),
    )


def _quota(session, owner_id, settings, required=1):
    active = session.scalar(
        select(func.count())
        .select_from(ResearchRun)
        .where(ResearchRun.owner_id == owner_id, ResearchRun.status.not_in(TERMINAL))
    )
    if active + required > quota_limit(session, owner_id, settings):
        raise DomainError(
            "run_quota", "Independent arms exceed available active-run quota", 429
        )


def reject_control_key(session, owner_id, key):
    if session.scalar(
        select(ControlledExperiment.id).where(
            ControlledExperiment.owner_id == owner_id,
            ControlledExperiment.idempotency_key == key,
        )
    ):
        raise DomainError(
            "idempotency_conflict", "This key belongs to a controlled experiment", 409
        )


def _key_lock(session, owner_id, key):
    from backend.app.comparison_services import reject_comparison_key
    from backend.app.final_evaluation_services import _idempotency_key

    _idempotency_key(key)
    advisory_lock(session, f"submit:{owner_id}")
    reject_comparison_key(session, owner_id, key)


def _selected_strategy(session, owner_id, source, *, name, code, parameters):
    raw = code.encode("utf-8")
    if not raw or len(raw) > 900_000:
        raise DomainError(
            "invalid_strategy", "Independent source exceeds bounded UTF-8 contract", 422
        )
    sha = hashlib.sha256(raw).hexdigest()
    original = source.source_config
    portable = original.schema_version.startswith("tradegrad.portable.")
    manifest = (
        json.loads(canonical_bytes(source.manifest))
        if source.manifest is not None
        else None
    )
    parameters = source.parameters if parameters is None else parameters
    if portable:
        from packages.strategy_sdk import hash_json, parse_manifest

        manifest.update(
            code_sha256=sha,
            parameters_sha256=hash_json(parameters),
            reference_result_sha256=None,
        )
        manifest = parse_manifest(canonical_bytes(manifest)).model_dump(mode="json")
    elif manifest is not None:
        if not isinstance(parameters, dict) or set(parameters) != set(
            source.parameters
        ):
            raise DomainError(
                "invalid_parameters",
                "Only declared business parameter values may change",
                422,
            )
        for declaration in manifest["parameters"]:
            declaration["value"] = parameters[declaration["name"]]
        try:
            manifest = NativeStrategyContract.model_validate(manifest).model_dump(
                mode="json"
            )
        except ValueError as error:
            raise DomainError("invalid_parameters", str(error), 422) from None
    elif parameters not in (None, {}):
        raise DomainError(
            "invalid_parameters", "Source has no declared business parameters", 422
        )
    strategy = StrategyVersion(
        id=uuid4(),
        owner_id=owner_id,
        name=name,
        task=original.task,
        mode="portable" if portable else "native_akquant",
        code=code,
        content_sha256=sha,
        parent_id=source.result.run.strategy_version_id,
        manifest=manifest,
        parameters=parameters,
    )
    session.add(strategy)
    session.flush()
    return strategy


def _make_config(source, strategy, independent_id, purpose, body):
    original = source.source_config
    values = {
        "independent_id": independent_id,
        "purpose": purpose,
        "source": body.source,
        "source_run_id": source.result.run.id,
        "original_config_sha256": source.result.run.request_sha256,
        "source_config": original,
        "source_config_sha256": original.sha256,
        "selected_strategy": VersionRef(
            id=str(strategy.id), sha256=strategy.content_sha256
        ),
        "split": source.result.evaluation.split,
        "reconstruction_window": DateWindow(
            start=source.result.evaluation.window["start"],
            end=source.result.evaluation.window["end"],
        ),
        "research_capture": body.research_capture,
        "environment_digest": original.environment_digest,
        "research_image_digest": source.result.run.research_image_digest,
        "runtime_observation_sha256": source.observation_sha256,
        "rng_restore": source.observation["rng"]["state"]
        if source.observation
        else None,
        "protocol_sha256": digest(source.protocol),
        "max_runtime_seconds": body.max_runtime_seconds,
    }
    if original.schema_version.startswith("tradegrad.portable."):
        from packages.strategy_sdk import hash_json

        return PortableIndependentEvaluationConfig(
            **values,
            selected_manifest=VersionRef(
                id=f"{strategy.id}/manifest", sha256=hash_json(strategy.manifest)
            ),
            selected_parameters=VersionRef(
                id=f"{strategy.id}/parameters", sha256=hash_json(strategy.parameters)
            ),
            portable_policy_image_digest=source.result.run.portable_policy_image_digest,
        )
    return IndependentEvaluationConfig(**values, selected_contract=strategy.manifest)


def record_independent_admission(session, run, item, source):
    config = independent_config(run)
    split = owned_resource(session, SplitVersion, run.split_version_id, run.owner_id)
    snapshot = get_exposure_snapshot(session, run.owner_id, split)
    record_exposure(
        session,
        owner_id=run.owner_id,
        dataset=source.result.dataset,
        window=DateWindow(
            start=source.result.dataset.start_date, end=config.reconstruction_window.end
        ),
        purpose="independent_evaluation_admission",
        source_key=f"independent:{run.id}:{run.generation}",
        run=run,
        provenance={
            "generation": run.generation,
            "config_sha256": config.sha256,
            "source_evaluation_id": str(item.source_evaluation_id),
            "source_result_sha256": config.source.result_manifest_sha256,
            "protocol_sha256": config.protocol_sha256,
            "admission_before_dispatch": True,
            "requested_window": config.window,
            "coverage": "conservative_dataset_prefix",
            "prior_exposure_snapshot": snapshot,
        },
    )
    from backend.app.benchmark_services import record_benchmark_admission

    record_benchmark_admission(session, run, config)
    return snapshot


def _create_one(
    session,
    owner_id,
    source,
    strategy,
    body,
    key,
    purpose,
    experiment_id,
    split_id,
    settings,
):
    identifier = uuid4()
    config = _make_config(source, strategy, identifier, purpose, body)
    run = ResearchRun(
        id=uuid4(),
        owner_id=owner_id,
        idempotency_key=key,
        request_sha256=config.sha256,
        config=config.model_dump(mode="json"),
        dataset_version_id=source.result.dataset.id,
        strategy_version_id=strategy.id,
        experiment_id=experiment_id,
        split_version_id=split_id,
        research_image_digest=config.research_image_digest,
        portable_policy_image_digest=getattr(
            config, "portable_policy_image_digest", None
        ),
        latest_checkpoint_id=None,
        resume_checkpoint_id=None,
        status="queued",
        generation=1,
        next_event_seq=0,
    )
    session.add(run)
    session.flush()
    item = IndependentEvaluation(
        id=identifier,
        owner_id=owner_id,
        run_id=run.id,
        source_evaluation_id=body.source.evaluation_id,
        purpose=purpose,
        request=body.model_dump(mode="json"),
        request_sha256=body.sha256,
        source_snapshot=source.snapshot,
        source_snapshot_sha256=digest(source.snapshot),
        protocol=source.protocol,
        protocol_sha256=config.protocol_sha256,
    )
    session.add(item)
    session.flush()
    validate_independent_inputs(session, run, settings=settings, resolved_source=source)
    record_independent_admission(session, run, item, source)
    session.add(OutboxEvent(run_id=run.id, generation=1))
    append_event(
        session,
        run,
        "independent_evaluation_admitted",
        {
            "status": "queued",
            "purpose": purpose,
            "independent_id": str(identifier),
            "source_evaluation_id": str(item.source_evaluation_id),
            "config_sha256": config.sha256,
        },
    )
    session.flush()
    return item, run


def validate_new_source_depth(session, owner_id, evaluation_id):
    """Budget the prospective leaf before admitting work that could not be read."""
    seen = set()
    current = evaluation_id
    for _ in range(MAX_INDEPENDENT_CHAIN):
        if current in seen:
            raise _integrity()
        seen.add(current)
        pair = session.execute(
            select(Evaluation, ResearchRun)
            .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
            .where(Evaluation.id == current, ResearchRun.owner_id == owner_id)
        ).one_or_none()
        if pair is None:
            raise DomainError("not_found", "Source evaluation not found", 404)
        _, run = pair
        if run.config.get("schema_version") not in SCHEMAS:
            return
        config = independent_config(run)
        current = config.source.evaluation_id
    raise DomainError(
        "independent_source_depth",
        "This source already reaches the bounded provenance depth; no unreadable descendant was created",
        422,
    )


def submit_diagnostic(session, owner_id, body, key, settings):
    _key_lock(session, owner_id, key)
    reject_control_key(session, owner_id, key)
    previous = session.scalar(
        select(ResearchRun).where(
            ResearchRun.owner_id == owner_id, ResearchRun.idempotency_key == key
        )
    )
    if previous:
        item, run = owned_independent(session, previous.id, owner_id)
        if item.purpose != "diagnostic" or item.request_sha256 != body.sha256:
            raise DomainError(
                "idempotency_conflict",
                "This key belongs to another independent request",
                409,
            )
        return item, run
    validate_new_source_depth(session, owner_id, body.source.evaluation_id)
    source = resolve_source(session, owner_id, body.source, settings)
    if source.unavailable:
        raise DomainError(
            "independent_source_unavailable", "; ".join(source.unavailable), 422
        )
    _quota(session, owner_id, settings)
    split = owned_resource(
        session, SplitVersion, source.result.run.split_version_id, owner_id
    )
    lock_split(session, owner_id, split)
    strategy = _selected_strategy(
        session,
        owner_id,
        source,
        name="Diagnostic source copy",
        code=source.admitted.source.code,
        parameters=source.parameters,
    )
    return _create_one(
        session,
        owner_id,
        source,
        strategy,
        body,
        key,
        "diagnostic",
        split.experiment_id,
        split.id,
        settings,
    )


def submit_controlled(session, owner_id, body, key, settings):
    from backend.app.final_evaluation_services import _idempotency_key

    _idempotency_key(key)
    advisory_lock(session, f"submit:{owner_id}")
    previous = session.scalar(
        select(ControlledExperiment).where(
            ControlledExperiment.owner_id == owner_id,
            ControlledExperiment.idempotency_key == key,
        )
    )
    if previous:
        if previous.request_sha256 != body.sha256:
            raise DomainError(
                "idempotency_conflict",
                "This key belongs to another control protocol",
                409,
            )
        return previous
    _key_lock(session, owner_id, key)
    if session.scalar(
        select(ResearchRun.id).where(
            ResearchRun.owner_id == owner_id, ResearchRun.idempotency_key == key
        )
    ):
        raise DomainError(
            "idempotency_conflict", "This key belongs to another run", 409
        )
    validate_new_source_depth(session, owner_id, body.source.evaluation_id)
    source = resolve_source(session, owner_id, body.source, settings)
    if source.unavailable or source.result.evaluation.split == "holdout":
        raise DomainError(
            "controlled_source_unavailable",
            "; ".join(source.unavailable)
            or "Controls are restricted to development/train or validation leaves",
            422,
        )
    if source.observation is None:
        raise DomainError(
            "controlled_rng_unavailable",
            "Fixed-condition controls require recorded source RNG state; historical unknown RNG cannot become a shared-state claim",
            422,
        )
    first = body.arms[0]
    if (
        first.code != source.admitted.source.code
        or (source.parameters if first.parameters is None else first.parameters)
        != source.parameters
    ):
        raise DomainError(
            "baseline_required",
            "The first control arm must copy exact original code and business parameters",
            422,
        )
    _quota(session, owner_id, settings, len(body.arms))
    parent_split = owned_resource(
        session, SplitVersion, source.result.run.split_version_id, owner_id
    )
    lock_split(session, owner_id, parent_split)
    child = create_experiment(
        session,
        owner_id,
        ExperimentCreate(
            name=body.name, parent_experiment_id=source.result.run.experiment_id
        ),
    )
    split = create_split_version(
        session,
        owner_id,
        SplitVersionCreate(
            experiment_id=child.id, definition=split_definition(parent_split)
        ),
    )
    item = ControlledExperiment(
        id=uuid4(),
        owner_id=owner_id,
        idempotency_key=key,
        experiment_id=child.id,
        split_version_id=split.id,
        source_evaluation_id=body.source.evaluation_id,
        request_sha256=body.sha256,
        request=body.model_dump(mode="json"),
        protocol=source.protocol,
        protocol_sha256=digest(source.protocol),
        exposure_snapshot=get_exposure_snapshot(session, owner_id, split),
    )
    session.add(item)
    session.flush()
    request = DiagnosticReplayCreate(
        source=body.source,
        research_capture=body.research_capture,
        max_runtime_seconds=body.max_runtime_seconds,
    )
    for ordinal, arm in enumerate(body.arms):
        strategy = _selected_strategy(
            session,
            owner_id,
            source,
            name=arm.name,
            code=arm.code,
            parameters=arm.parameters,
        )
        execution, _ = _create_one(
            session,
            owner_id,
            source,
            strategy,
            request,
            f"control:{item.id}:{ordinal}",
            "controlled",
            child.id,
            split.id,
            settings,
        )
        changed = [] if arm.code == source.admitted.source.code else ["strategy_code"]
        changed += [
            f"parameters.{name}"
            for name in sorted(
                set(source.parameters or {}) | set(strategy.parameters or {})
            )
            if (source.parameters or {}).get(name)
            != (strategy.parameters or {}).get(name)
        ]
        interpretation = (
            "baseline"
            if ordinal == 0
            else "unchanged"
            if not changed
            else "single_parameter_change_not_causal_proof"
            if len(changed) == 1 and changed[0].startswith("parameters.")
            else "not_individually_validated"
        )
        session.add(
            ControlledArm(
                id=uuid4(),
                controlled_experiment_id=item.id,
                independent_id=execution.id,
                ordinal=ordinal,
                name=arm.name,
                planned_change=arm.planned_change,
                changed_fields=changed,
                interpretation=interpretation,
            )
        )
    session.flush()
    return item


def owned_independent(session, run_id, owner_id, *, lock=False):
    run = owned_run(session, run_id, owner_id, lock=lock)
    item = session.scalar(
        select(IndependentEvaluation).where(
            IndependentEvaluation.run_id == run.id,
            IndependentEvaluation.owner_id == owner_id,
        )
    )
    if item is None:
        raise DomainError("not_found", "Independent evaluation not found", 404)
    config = independent_config(run)
    if (
        item.id != config.independent_id
        or item.purpose != config.purpose
        or item.source_evaluation_id != config.source.evaluation_id
        or item.request_sha256 != digest(item.request)
        or item.source_snapshot_sha256 != digest(item.source_snapshot)
        or item.protocol_sha256 != digest(item.protocol)
        or item.protocol_sha256 != config.protocol_sha256
    ):
        raise _integrity()
    return item, run


def validate_independent_inputs(
    session, run, settings, *, resolved_source=None, execution=True
):
    item, _ = owned_independent(session, run.id, run.owner_id)
    config = independent_config(run)
    source = resolved_source or resolve_source(
        session, run.owner_id, config.source, settings
    )
    strategy = owned_resource(
        session, StrategyVersion, run.strategy_version_id, run.owner_id
    )
    split = owned_resource(session, SplitVersion, run.split_version_id, run.owner_id)
    original_split = owned_resource(
        session, SplitVersion, source.result.run.split_version_id, run.owner_id
    )
    if (
        source.snapshot != item.source_snapshot
        or digest(source.protocol) != item.protocol_sha256
        or config.source_config != source.source_config
        or config.original_config_sha256 != source.result.run.request_sha256
        or config.source_run_id != source.result.run.id
        or config.window != source.result.evaluation.window
        or config.runtime_observation_sha256 != source.observation_sha256
        or config.rng_restore
        != (source.observation["rng"]["state"] if source.observation else None)
        or run.dataset_version_id != source.result.dataset.id
        or strategy.task != source.source_config.task
        or strategy.mode != config.evaluation_mode
        or run.experiment_id != split.experiment_id
        or split_definition(split) != split_definition(original_split)
        or run.research_image_digest != config.research_image_digest
        or run.research_image_digest != source.result.run.research_image_digest
        or run.portable_policy_image_digest
        != source.result.run.portable_policy_image_digest
        or config.selected_strategy.id != str(strategy.id)
        or config.selected_strategy.sha256 != strategy.content_sha256
        or hashlib.sha256(strategy.code.encode()).hexdigest() != strategy.content_sha256
        or run.latest_checkpoint_id is not None
        or run.resume_checkpoint_id is not None
        or run.final_selection_id is not None
    ):
        raise _integrity()
    if execution and source.unavailable:
        raise DomainError(
            "independent_source_unavailable", "; ".join(source.unavailable), 422
        )
    if config.purpose == "diagnostic" and (
        strategy.code != source.admitted.source.code
        or strategy.parameters != source.parameters
        or run.experiment_id != source.result.run.experiment_id
        or run.split_version_id != source.result.run.split_version_id
    ):
        raise _integrity()
    if config.purpose == "controlled" and resolved_source is None:
        # Construction validates the selected strategy before its arm is inserted.
        # Every later dispatch/read/completion requires the complete frozen family.
        arm = session.scalar(
            select(ControlledArm).where(ControlledArm.independent_id == item.id)
        )
        control = (
            session.get(ControlledExperiment, arm.controlled_experiment_id)
            if arm is not None
            else None
        )
        child = session.get(Experiment, run.experiment_id)
        parent = session.get(Experiment, source.result.run.experiment_id)
        if (
            control is None
            or child is None
            or parent is None
            or control.owner_id != run.owner_id
            or control.experiment_id != run.experiment_id
            or control.split_version_id != run.split_version_id
            or control.source_evaluation_id != item.source_evaluation_id
            or control.protocol_sha256 != item.protocol_sha256
            or control.request_sha256 != digest(control.request)
            or control.protocol_sha256 != digest(control.protocol)
            or child.parent_experiment_id != parent.id
            or child.root_experiment_id != (parent.root_experiment_id or parent.id)
            or config.split not in {"train", "validation"}
        ):
            raise _integrity()
        from backend.app.independent_schemas import ControlledExperimentCreate

        body = ControlledExperimentCreate.model_validate(control.request)
        ordinals = session.scalars(
            select(ControlledArm.ordinal)
            .where(ControlledArm.controlled_experiment_id == control.id)
            .order_by(ControlledArm.ordinal)
        ).all()
        changed = (
            [] if strategy.code == source.admitted.source.code else ["strategy_code"]
        )
        changed += [
            f"parameters.{name}"
            for name in sorted(
                set(source.parameters or {}) | set(strategy.parameters or {})
            )
            if (source.parameters or {}).get(name)
            != (strategy.parameters or {}).get(name)
        ]
        interpretation = (
            "baseline"
            if arm.ordinal == 0
            else "unchanged"
            if not changed
            else "single_parameter_change_not_causal_proof"
            if len(changed) == 1 and changed[0].startswith("parameters.")
            else "not_individually_validated"
        )
        if (
            ordinals != list(range(len(body.arms)))
            or arm.changed_fields != changed
            or arm.interpretation != interpretation
            or body.arms[0].code != source.admitted.source.code
            or (
                source.parameters
                if body.arms[0].parameters is None
                else body.arms[0].parameters
            )
            != source.parameters
        ):
            raise _integrity()
        expected = body.arms[arm.ordinal] if 0 <= arm.ordinal < len(body.arms) else None
        if (
            expected is None
            or body.source != config.source
            or expected.code != strategy.code
            or (
                source.parameters
                if expected.parameters is None
                else expected.parameters
            )
            != strategy.parameters
            or expected.name != arm.name
            or expected.planned_change != arm.planned_change
            or body.research_capture != config.research_capture
            or body.max_runtime_seconds != config.max_runtime_seconds
        ):
            raise _integrity()
    if config.schema_version == NATIVE_SCHEMA:
        from packages.tradegrad_adapter.native_contract import validate_native_binding

        validate_native_binding(
            config.selected_contract, strategy.manifest, strategy.parameters
        )
    else:
        from workers.research.independent_staging import (
            validate_portable_independent_binding,
        )

        validate_portable_independent_binding(
            config, strategy.manifest, strategy.parameters
        )
    if settings is not None and execution:
        from backend.app.benchmark_services import preflight_benchmarks
        from workers.research.independent_staging import preflight_independent_window

        try:
            preflight_independent_window(config, source.result.dataset, settings)
            preflight_benchmarks(
                session,
                run.owner_id,
                config,
                source.result.dataset.manifest,
                [config.window],
                settings,
            )
        except (ValueError, OSError):
            raise DomainError(
                "independent_preflight_failed",
                "Original window and warmup cannot be reconstructed from the immutable inputs",
                422,
            ) from None
    return item, source, source.result.dataset, strategy, split


def require_independent_admission(session, run, evaluation=None):
    """Authenticate the complete original writer contract, not just a source key."""
    from backend.app.experiment_schemas import ExposureSnapshot
    from backend.app.experiment_services import _identities

    config = independent_config(run)
    key = f"independent:{run.id}:{run.generation}"
    admission = session.scalar(
        select(DataUsageExposure).where(
            DataUsageExposure.owner_id == run.owner_id,
            DataUsageExposure.source_key == key,
        )
    )
    dataset = session.get(DatasetVersion, run.dataset_version_id)
    try:
        if (
            admission is None
            or dataset is None
            or admission.owner_id != run.owner_id
            or admission.source_key != key
            or admission.run_id != run.id
            or admission.experiment_id != run.experiment_id
            or admission.split_version_id != run.split_version_id
            or admission.dataset_version_id != run.dataset_version_id
            or dataset.id != run.dataset_version_id
            or dataset.owner_id != run.owner_id
            or str(dataset.id) != config.source_config.dataset.id
            or dataset.content_sha256 != config.source_config.dataset.sha256
            or admission.start_date != dataset.start_date
            or admission.end_date != config.reconstruction_window.end
            or admission.identities != _identities(session, dataset)
            or admission.purpose != "independent_evaluation_admission"
            or not isinstance(admission.provenance, dict)
            or evaluation is not None
            and (evaluation.run_id != run.id or evaluation.generation != run.generation)
        ):
            raise ValueError("Admission resource binding differs")
        p = admission.provenance
        if (
            type(p.get("generation")) is not int
            or p["generation"] != run.generation
            or p.get("config_sha256") != config.sha256
            or p.get("source_evaluation_id") != str(config.source.evaluation_id)
            or p.get("source_result_sha256") != config.source.result_manifest_sha256
            or p.get("protocol_sha256") != config.protocol_sha256
            or p.get("requested_window") != config.window
            or p.get("coverage") != "conservative_dataset_prefix"
            or p.get("admission_before_dispatch") is not True
        ):
            raise ValueError("Exact-generation admission provenance differs")
        bounded_json(p["prior_exposure_snapshot"], 256000)
        ExposureSnapshot.model_validate(p["prior_exposure_snapshot"])
        return admission
    except (ValueError, TypeError, KeyError, AttributeError, DomainError):
        raise DomainError(
            "admission_required",
            "Exact-generation independent exposure admission is missing or invalid",
            409,
        ) from None


def _check_evaluation(run, config, item, attempt):
    if (
        item.run_id != run.id
        or item.attempt_id != attempt.id
        or item.generation != attempt.generation
        or item.generation != run.generation
        or item.window != config.window
        or item.window_sha256 != payload_hash(config.window)
        or item.split != config.split
        or item.code_sha256 != config.selected_strategy.sha256
        or item.image_digest != run.research_image_digest
        or item.candidate_id is not None
        or item.candidate_key != f"independent:{config.independent_id}"
    ):
        raise _integrity()


def begin_independent_evaluation(session, attempt_id, generation, token):
    run, attempt = fenced_attempt(session, attempt_id, generation, token)
    config = independent_config(run)
    owned_independent(session, run.id, run.owner_id)
    require_independent_admission(session, run)
    if run.status == "cancel_requested":
        raise DomainError(
            "cancel_requested", "Cancellation prevents another sandbox", 409
        )
    previous = session.scalar(
        select(Evaluation).where(Evaluation.attempt_id == attempt.id)
    )
    if previous:
        _check_evaluation(run, config, previous, attempt)
        return previous
    identifier = uuid4()
    item = Evaluation(
        id=identifier,
        run_id=run.id,
        attempt_id=attempt.id,
        generation=generation,
        candidate_id=None,
        candidate_key=f"independent:{config.independent_id}",
        code_sha256=config.selected_strategy.sha256,
        split=config.split,
        window=config.window,
        window_sha256=payload_hash(config.window),
        image_digest=run.research_image_digest,
        sandbox_id=f"tradegrad-{identifier}",
        sandbox_stopped=False,
        status="registered",
    )
    session.add(item)
    session.flush()
    return item


def complete_independent_evaluation(
    session,
    attempt_id,
    generation,
    token,
    evaluation_id,
    *,
    outcome,
    sandbox_stopped,
    settings,
):
    run, attempt = fenced_attempt(session, attempt_id, generation, token)
    config = independent_config(run)
    item = session.scalar(
        select(Evaluation)
        .where(
            Evaluation.id == evaluation_id,
            Evaluation.run_id == run.id,
            Evaluation.attempt_id == attempt.id,
        )
        .with_for_update()
    )
    if item is None:
        raise DomainError("not_found", "Registered independent leaf not found", 404)
    _check_evaluation(run, config, item, attempt)
    require_independent_admission(session, run, item)
    raw = bounded_json(outcome)
    sha = hashlib.sha256(raw).hexdigest()
    if item.finished_at is not None:
        if item.payload_sha256 != sha or item.sandbox_stopped != sandbox_stopped:
            raise DomainError(
                "immutable_evaluation",
                "Completed independent evidence cannot change",
                409,
            )
        return item
    if not sandbox_stopped:
        item.error = "Independent sandbox termination unconfirmed; recovery blocked"
        append_event(
            session,
            run,
            "recovery_blocked",
            {"evaluation_id": str(item.id), "message": item.error},
            attempt.id,
        )
        return item
    status = outcome.get("status", "failed")
    if status not in {"succeeded", "failed", "cancelled", "interrupted"}:
        status = "failed"
    if status == "succeeded":
        try:
            validate_independent_inputs(session, run, settings, execution=False)
            from backend.app.independent_views import validate_independent_payload

            session.flush()
            validate_independent_payload(session, run, item, outcome, settings)
        except (ValueError, TypeError, KeyError, DomainError):
            status = "failed"
    item.status, item.payload, item.payload_sha256 = status, json.loads(raw), sha
    item.error = (
        redact(str(outcome["error"]))[:4000]
        if outcome.get("error")
        else "Independent result evidence is incomplete or invalid"
        if status != outcome.get("status")
        else None
    )
    item.sandbox_stopped, item.finished_at = True, utcnow()
    append_event(
        session,
        run,
        "evaluation_completed",
        {"evaluation_id": str(item.id), "status": status, "payload_sha256": sha},
        attempt.id,
    )
    session.flush()
    return item


def finish_independent_attempt(
    session, attempt_id, generation, token, outcome, sandbox_stopped
):
    run, attempt = fenced_attempt(session, attempt_id, generation, token)
    config = independent_config(run)
    require_independent_admission(session, run)
    items = session.scalars(
        select(Evaluation).where(Evaluation.attempt_id == attempt.id)
    ).all()
    if not sandbox_stopped or any(not item.sandbox_stopped for item in items):
        run.error = attempt.error = (
            "Recovery blocked: independent sandbox termination unconfirmed"
        )
        append_event(
            session, run, "recovery_blocked", {"message": run.error}, attempt.id
        )
        return run
    item = items[0] if len(items) == 1 else None
    if item:
        _check_evaluation(run, config, item, attempt)
    status = outcome.get("status", "failed")
    if status not in {"succeeded", "failed", "cancelled", "interrupted"}:
        status = "failed"
    if run.status == "cancel_requested":
        status = "cancelled"
    if status == "succeeded" and (
        item is None
        or item.status != "succeeded"
        or item.finished_at is None
        or outcome.get("evaluation_id") != str(item.id)
        or outcome.get("exit_code") != 0
    ):
        status = "failed"
    attempt.status = run.status = status
    attempt.sandbox_stopped, attempt.finished_at, attempt.exit_code = (
        True,
        utcnow(),
        outcome.get("exit_code"),
    )
    attempt.error = run.error = (
        redact(str(outcome["error"]))[:4000]
        if outcome.get("error")
        else "Independent result was not durably completed"
        if status == "failed"
        else None
    )
    # Status-only run payload. Metrics never leak through ordinary polling or SSE.
    run.result = {
        "execution_mode": "independent_evaluation",
        "purpose": config.purpose,
        "evaluation_id": str(item.id) if item else None,
    }
    append_event(
        session,
        run,
        "run_finished",
        {
            "status": status,
            "execution_mode": "independent_evaluation",
            "evaluation_id": str(item.id) if item else None,
        },
        attempt.id,
    )
    session.flush()
    return run


def retry_independent(session, run_id, owner_id, settings, key):
    from backend.app.final_evaluation_services import _idempotency_key

    _idempotency_key(key)
    advisory_lock(session, f"submit:{owner_id}")
    item, run = owned_independent(session, run_id, owner_id, lock=True)
    if session.scalar(
        select(RunEvent.id).where(
            RunEvent.run_id == run.id,
            RunEvent.type == "independent_retry_admitted",
            RunEvent.payload["idempotency_key"].astext == key,
        )
    ):
        return item, run
    if run.status not in {"failed", "cancelled", "interrupted"}:
        raise DomainError(
            "invalid_transition",
            "Only stopped failed/cancelled/interrupted independent runs can retry",
            409,
        )
    attempts = session.scalars(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.run_id == run.id)
        .with_for_update()
    ).all()
    pending = session.scalar(
        select(Evaluation.id)
        .where(Evaluation.run_id == run.id, Evaluation.sandbox_stopped.is_(False))
        .limit(1)
    )
    if pending or any(
        not attempt.sandbox_stopped or attempt.finished_at is None
        for attempt in attempts
    ):
        raise DomainError(
            "sandbox_stop_required",
            "Retry requires confirmed termination of every earlier sandbox",
            409,
        )
    _, source, _, _, split = validate_independent_inputs(session, run, settings)
    lock_split(session, owner_id, split)
    _quota(session, owner_id, settings)
    run.generation += 1
    run.active_attempt_id, run.status, run.error, run.result = (
        None,
        "queued",
        None,
        None,
    )
    record_independent_admission(session, run, item, source)
    session.add(OutboxEvent(run_id=run.id, generation=run.generation))
    append_event(
        session,
        run,
        "independent_retry_admitted",
        {
            "idempotency_key": key,
            "status": "queued",
            "generation": run.generation,
            "retry_mode": "new_physical_attempt_from_original_start",
        },
    )
    session.flush()
    return item, run
