"""Portable admission and durable evidence, separate from native evaluation trust."""

from __future__ import annotations

import hashlib
import json
import re
from uuid import UUID, uuid4

from sqlalchemy import func, select

from backend.app.models import (
    Artifact,
    DatasetVersion,
    Evaluation,
    ExecutionAttempt,
    OutboxEvent,
    ResearchRun,
    StrategyVersion,
    UniverseVersion,
    utcnow,
)
from backend.app.research_services import (
    _key,
    _reference,
    _sha,
    bounded_json,
    latest_checkpoint_for_resume,
    payload_hash,
    validate_model_selection,
)
from backend.app.services import (
    TERMINAL,
    DomainError,
    advisory_lock,
    append_event,
    fenced_attempt,
    owned_run,
)
from backend.app.storage import read_verified, redact
from backend.app.workspace_policy import enforce_model_policy, quota_limit
from packages.strategy_sdk import (
    DataRequirements,
    EngineProfile,
    PriceAssumptions,
    StateCodec,
    StateField,
    StateSchema,
    StrategyManifest,
    UniverseAssumptions,
    canonical_json,
    hash_json,
    parse_manifest,
)
from packages.tradegrad_adapter.portable_config import (
    PortableRunConfig,
    portable_evaluation_windows,
)
from packages.tradegrad_adapter.portable_prompts import (
    PROMPT_VERSION,
    template_source,
    validate_policy_source,
    validate_user_policy_source,
)


def portable_config(run):
    try:
        return PortableRunConfig.model_validate(run.config)
    except (ValueError, TypeError):
        raise DomainError(
            "invalid_portable_config", "Run is not an immutable portable run", 409
        ) from None


def owned_portable_run(session, run_id, owner_id, lock=False):
    run = owned_run(session, run_id, owner_id, lock)
    portable_config(run)
    return run


def _dataset_universe(session, owner_id, dataset_ref, universe_ref):
    dataset = _reference(session, DatasetVersion, dataset_ref, owner_id)
    universe = _reference(session, UniverseVersion, universe_ref, owner_id)
    listings = {str(row["listing_id"]) for row in universe.members}
    if not listings or listings != {
        str(row["listing_id"]) for row in dataset.manifest.get("listings", [])
    }:
        raise DomainError(
            "universe_mismatch", "Dataset and universe listings must match exactly", 422
        )
    pin = dataset.manifest.get("universe", {})
    if pin.get("id") and (
        pin["id"] != str(universe.id)
        or pin.get("content_sha256") != universe.content_sha256
    ):
        raise DomainError(
            "universe_mismatch", "Dataset pins another universe version", 422
        )
    return dataset, universe


def reference_state(kind):
    fields = [StateField(name="last_session", kind="string")]
    state = {"last_session": ""}
    if kind == "portfolio":
        fields.append(StateField(name="decisions", kind="integer"))
        state["decisions"] = 0
    schema = StateSchema(
        name="momentum" if kind == "portfolio" else "trend",
        version=1,
        fields=tuple(fields),
    )
    return schema, StateCodec(schema).encode(state)


def create_portable_strategy(session, owner_id, body):
    from packages.tradegrad_adapter.portable_environment import (
        portable_environment_digest,
    )
    from runtimes.portable_engine.runtime import profile_sha256

    dataset, universe = _dataset_universe(
        session, owner_id, body.dataset, body.universe
    )
    if universe.state != "user_fixed" or not 1 <= len(universe.members) <= 100:
        raise DomainError(
            "portable_universe_ineligible",
            "Portable reference policies require 1 to 100 explicitly fixed listings",
            422,
        )
    source = template_source(body.strategy_kind)
    validate_policy_source(source, body.strategy_kind)
    identifier = uuid4()
    market = dataset.market
    params = (
        {"top_n": min(3, len(universe.members))}
        if body.strategy_kind == "portfolio"
        else {"quantity": 100 if market == "CN" else 1}
    )
    schema, _state = reference_state(body.strategy_kind)
    actions = dataset.manifest.get("artifact_files", {}).get("actions.json")
    if not actions:
        raise DomainError(
            "portable_dataset_ineligible",
            "Dataset must include immutable corporate action evidence",
            422,
        )
    manifest = StrategyManifest(
        strategy_kind=body.strategy_kind,
        strategy_id=str(identifier),
        code_sha256=hashlib.sha256(source.encode()).hexdigest(),
        parameters_sha256=hash_json(params),
        market=market,
        currency="CNY" if market == "CN" else "USD",
        calendar="XSHG" if market == "CN" else "XNYS",
        timezone="Asia/Shanghai" if market == "CN" else "America/New_York",
        data=DataRequirements(
            lookback_bars=2, warmup_bars=2, max_listings=len(universe.members)
        ),
        universe=UniverseAssumptions(
            semantics=universe.state,
            snapshot_sha256=universe.content_sha256,
            survivorship_bias="membership_evidence_required"
            if universe.state == "point_in_time_verified"
            else "possible",
        ),
        prices=PriceAssumptions(actions_snapshot_sha256=actions),
        state_schema=schema,
        engine_profile=EngineProfile(
            profile_sha256=profile_sha256(market, body.initial_cash),
            environment_sha256=portable_environment_digest(),
        ),
    )
    item = StrategyVersion(
        id=identifier,
        owner_id=owner_id,
        name=body.name.strip(),
        task=f"{market.lower()}_{'cross' if body.strategy_kind == 'portfolio' else 'time'}",
        mode="portable",
        content_sha256=manifest.code_sha256,
        code=source,
        manifest=manifest.model_dump(mode="json"),
        parameters=params,
    )
    session.add(item)
    session.flush()
    return item


def portable_defaults(session, owner_id, body):
    from packages.tradegrad_adapter.config import OptimizerConfig, WarmupConfig
    from packages.tradegrad_adapter.portable_environment import (
        portable_environment_digest,
    )

    dataset, _ = _dataset_universe(session, owner_id, body.dataset, body.universe)
    strategy = _reference(session, StrategyVersion, body.seed_strategy, owner_id)
    if (
        strategy.mode != "portable"
        or strategy.manifest is None
        or strategy.parameters is None
    ):
        raise DomainError(
            "reference_mismatch", "Choose an immutable portable strategy", 422
        )
    manifest = parse_manifest(canonical_json(strategy.manifest))
    schema, state = reference_state(manifest.strategy_kind)
    if manifest.authoring is not None:
        state = manifest.authoring.initial_state
    elif manifest.state_schema != schema:
        raise DomainError(
            "reference_mismatch",
            "Reference policy state schema differs from reviewed template",
            422,
        )
    # Cash is bound through the exact profile hash, not inferred from market defaults.
    from runtimes.portable_engine.runtime import profile_sha256

    initial_cash = 1_000_000
    if manifest.engine_profile.profile_sha256 != profile_sha256(
        dataset.market, initial_cash
    ):
        raise DomainError(
            "explicit_cash_required",
            "Nondefault cash requires an explicit portable configuration",
            422,
        )
    return PortableRunConfig(
        task=strategy.task,
        strategy_kind=manifest.strategy_kind,
        execution_mode=body.execution_mode,
        dataset=body.dataset,
        universe=body.universe,
        seed_strategy=body.seed_strategy,
        seed_manifest={
            "id": f"{strategy.id}/manifest",
            "sha256": hash_json(strategy.manifest),
        },
        seed_parameters={
            "id": f"{strategy.id}/parameters",
            "sha256": hash_json(strategy.parameters),
        },
        windows=body.windows,
        warmup=WarmupConfig(
            sessions=max(manifest.data.warmup_bars, manifest.data.lookback_bars)
        ),
        initial_state=state,
        initial_cash=initial_cash,
        optimizer=OptimizerConfig(max_iterations=0)
        if body.execution_mode == "seed_evaluation"
        else OptimizerConfig(),
        environment_digest=portable_environment_digest(),
        prompt_version=PROMPT_VERSION,
    )


def validate_portable_submission(session, owner_id, body, settings):
    from packages.tradegrad_adapter.portable_environment import (
        portable_environment_digest,
    )
    from packages.tradegrad_adapter.portable_windows import preflight_portable_dataset

    if body.environment_digest != portable_environment_digest():
        raise DomainError(
            "environment_mismatch",
            "Portable environment differs from this deployment",
            422,
        )
    if body.prompt_version != PROMPT_VERSION:
        raise DomainError(
            "portable_capability_unavailable",
            "Only reviewed portable prompts are supported",
            422,
        )
    enforce_model_policy(session, owner_id, body.model)
    validate_model_selection(body.model, settings, owner_id)
    dataset, universe = _dataset_universe(
        session, owner_id, body.dataset, body.universe
    )
    strategy = _reference(session, StrategyVersion, body.seed_strategy, owner_id)
    try:
        if (
            dataset.market != body.market
            or strategy.task != body.task
            or strategy.mode != "portable"
        ):
            raise ValueError("Portable task, market and source mode must agree")
        parsed_manifest = parse_manifest(canonical_json(strategy.manifest))
        if parsed_manifest.authoring is None:
            validate_policy_source(strategy.code, body.strategy_kind)
        else:
            validate_user_policy_source(strategy.code, body.strategy_kind)
            if body.optimizer.max_iterations > 0:
                validate_policy_source(
                    strategy.code, body.strategy_kind, baseline=strategy.code
                )
        if (
            hashlib.sha256(strategy.code.encode()).hexdigest()
            != body.seed_strategy.sha256
        ):
            raise ValueError("Immutable policy source hash changed")
        manifest = parse_manifest(canonical_json(strategy.manifest))
        if (
            body.seed_manifest.id != f"{strategy.id}/manifest"
            or body.seed_parameters.id != f"{strategy.id}/parameters"
        ):
            raise ValueError(
                "Manifest and parameter references must belong to the seed strategy"
            )
        if (
            hash_json(strategy.manifest) != body.seed_manifest.sha256
            or hash_json(strategy.parameters) != body.seed_parameters.sha256
        ):
            raise ValueError("Immutable manifest or parameter hash differs")
        if (
            manifest.strategy_id != str(strategy.id)
            or manifest.code_sha256 != body.seed_strategy.sha256
            or manifest.parameters_sha256 != body.seed_parameters.sha256
            or manifest.strategy_kind != body.strategy_kind
            or manifest.market != body.market
            or manifest.universe.snapshot_sha256 != body.universe.sha256
            or manifest.engine_profile.environment_sha256 != body.environment_digest
            or manifest.engine_profile.profile_sha256 != body.engine_profile.sha256
        ):
            raise ValueError(
                "Manifest semantic references differ from immutable portable config"
            )
        from workers.research.portable_staging import validate_portable_binding

        validate_portable_binding(body, strategy.manifest, strategy.parameters)
        if manifest.prices.actions_snapshot_sha256 != dataset.manifest.get(
            "artifact_files", {}
        ).get("actions.json"):
            raise ValueError("Seed corporate-action snapshot differs from its dataset")
        StateCodec(manifest.state_schema).decode(body.initial_state)
        if body.warmup.sessions < max(
            manifest.data.warmup_bars, manifest.data.lookback_bars
        ):
            raise ValueError(
                "Configured warmup is below the immutable manifest requirement"
            )
        for _, window in body.windows.items():  # noqa: PERF102 - immutable WindowSet, not dict
            if window.start < dataset.start_date or window.end > dataset.end_date:
                raise ValueError("Configured windows exceed dataset coverage")
        hashes = dataset.manifest.get("files", {}) | dataset.manifest.get(
            "artifact_files", {}
        )

        def read_file(name):
            if name not in hashes:
                raise ValueError("Requested dataset file lacks immutable evidence")
            return read_verified(
                settings.storage_root,
                f"{dataset.storage_key}/{name}",
                hashes[name],
                settings.max_artifact_bytes,
            )

        reports = [
            preflight_portable_dataset(
                body,
                window["split"],
                dataset.manifest,
                read_file,
                evaluation_window=window,
            )
            for window in portable_evaluation_windows(body)
        ]
    except DomainError:
        raise
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise DomainError(
            "portable_preflight_failed", redact(str(error))[:2000], 422
        ) from None
    from backend.app.benchmark_services import preflight_benchmarks

    preflight_benchmarks(
        session,
        owner_id,
        body,
        dataset.manifest,
        portable_evaluation_windows(body),
        settings,
    )
    return dataset, strategy, universe, reports


def submit_portable_run(
    session,
    owner_id,
    body,
    idempotency_key,
    settings,
    *,
    experiment_id=None,
    split_version_id=None,
):
    if (
        not idempotency_key
        or len(idempotency_key) > 128
        or any(ord(c) < 33 or ord(c) > 126 for c in idempotency_key)
    ):
        raise DomainError(
            "invalid_idempotency_key",
            "Use 1 to 128 printable ASCII characters without spaces",
            422,
        )
    advisory_lock(session, f"submit:{owner_id}")
    from backend.app.comparison_services import reject_comparison_key

    reject_comparison_key(session, owner_id, idempotency_key)
    binding = {
        "experiment_id": str(experiment_id) if experiment_id else None,
        "split_version_id": str(split_version_id) if split_version_id else None,
    }
    previous = session.scalar(
        select(ResearchRun).where(
            ResearchRun.owner_id == owner_id,
            ResearchRun.idempotency_key == idempotency_key,
        )
    )
    if previous:
        if (
            previous.request_sha256 != body.sha256
            or previous.submission_binding != binding
        ):
            raise DomainError(
                "idempotency_conflict",
                "Key already identifies different immutable inputs",
            )
        portable_config(previous)
        return previous
    dataset, strategy, universe, _ = validate_portable_submission(
        session, owner_id, body, settings
    )
    active = session.scalar(
        select(func.count())
        .select_from(ResearchRun)
        .where(ResearchRun.owner_id == owner_id, ResearchRun.status.not_in(TERMINAL))
    )
    if active >= quota_limit(session, owner_id, settings):
        raise DomainError("run_quota", "Active run limit reached", 429)
    from backend.app.experiment_services import (
        bind_research_submission,
        record_research_exposure,
    )

    experiment, split = bind_research_submission(
        session,
        owner_id,
        body,
        dataset,
        universe,
        experiment_id=experiment_id,
        split_version_id=split_version_id,
    )
    run = ResearchRun(
        id=uuid4(),
        owner_id=owner_id,
        idempotency_key=idempotency_key,
        request_sha256=body.sha256,
        config=body.model_dump(mode="json"),
        submission_binding=binding,
        experiment_id=experiment.id,
        split_version_id=split.id,
        dataset_version_id=dataset.id,
        strategy_version_id=strategy.id,
        status="queued",
        generation=1,
        next_event_seq=0,
    )
    session.add(run)
    session.flush()
    record_research_exposure(session, run, dataset, universe, body)
    session.add(OutboxEvent(run_id=run.id, generation=1))
    append_event(
        session,
        run,
        "stage_changed",
        {
            "status": "queued",
            "evaluation_mode": "portable",
            "execution_mode": body.execution_mode,
            "config_sha256": body.sha256,
            "is_synthetic": dataset.is_synthetic,
        },
    )
    session.flush()
    return run


def pin_portable_images(session, attempt_id, generation, token, images):
    run, _ = fenced_attempt(session, attempt_id, generation, token)
    portable_config(run)
    if (
        type(images) is not dict
        or set(images) != {"engine", "policy"}
        or any(
            not isinstance(v, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", v)
            for v in images.values()
        )
    ):
        raise DomainError(
            "invalid_image_digest", "Pin both actual portable image IDs", 422
        )
    for attr, role in (
        ("research_image_digest", "engine"),
        ("portable_policy_image_digest", "policy"),
    ):
        previous = getattr(run, attr)
        if previous is not None and previous != images[role]:
            raise DomainError(
                "image_digest_mismatch",
                "Portable resume cannot change either sibling image",
            )
    run.research_image_digest, run.portable_policy_image_digest = (
        images["engine"],
        images["policy"],
    )
    return run


def begin_portable_evaluation(
    session, attempt_id, generation, token, *, candidate_key, code_sha256, window
):
    run, attempt = fenced_attempt(session, attempt_id, generation, token)
    config = portable_config(run)
    if run.status == "cancel_requested":
        raise DomainError(
            "cancel_requested", "Cancellation prevents another evaluation"
        )
    bounded_json(window, 4096)
    if window not in portable_evaluation_windows(config):
        raise DomainError(
            "invalid_evaluation_window",
            "Only exact portable train/validation windows may execute",
            422,
        )
    if not run.research_image_digest or not run.portable_policy_image_digest:
        raise DomainError(
            "image_digest_required", "Both verified sibling image IDs must be pinned"
        )
    identifier = uuid4()
    row = Evaluation(
        id=identifier,
        run_id=run.id,
        attempt_id=attempt.id,
        generation=generation,
        candidate_key=_key(candidate_key),
        code_sha256=_sha(code_sha256),
        split=window["split"],
        window=window,
        window_sha256=payload_hash(window),
        image_digest=run.research_image_digest,
        sandbox_id=f"tradegrad-portable-{identifier}",
        sandbox_stopped=False,
        status="registered",
    )
    session.add(row)
    session.flush()
    return row


def portable_score(bundle):
    summary = bundle["summary"]
    return round(
        float(summary["total_return_pct"]) - float(summary["max_drawdown_pct"]), 8
    )


def verify_portable_bundle_binding(
    bundle, config, evaluation, *, allow_pending_benchmark=False
):
    from packages.tradegrad_adapter.portable_results import validate_portable_result

    bundle = validate_portable_result(bundle)
    from packages.tradegrad_adapter.benchmarks import require_comparison_binding

    require_comparison_binding(bundle, config, allow_pending=allow_pending_benchmark)
    identity = bundle["identity"]
    window = bundle["window"]
    if (
        identity["run_config_sha256"] != config.sha256
        or identity["strategy_sha256"] != evaluation.code_sha256
        or identity["parameters_sha256"] != config.seed_parameters.sha256
        or window.get("start") != evaluation.window["start"]
        or window.get("end") != evaluation.window["end"]
        or bundle["manifest"]["strategy_kind"] != config.strategy_kind
        or bundle["manifest"]["market"] != config.market
        or bundle["manifest"]["strategy_id"] != config.seed_strategy.id
        or hash_json(bundle["manifest"]["state_schema"])
        != config.initial_state.schema_sha256
        or bundle["manifest"]["engine_profile"]["profile_sha256"]
        != config.engine_profile.sha256
        or bundle["manifest"]["engine_profile"]["environment_sha256"]
        != config.environment_digest
    ):
        raise ValueError("Portable result is not bound to this immutable evaluation")
    return bundle


def complete_portable_evaluation(
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
    config = portable_config(run)
    row = session.scalar(
        select(Evaluation)
        .where(
            Evaluation.id == evaluation_id,
            Evaluation.run_id == run.id,
            Evaluation.attempt_id == attempt.id,
        )
        .with_for_update()
    )
    if row is None:
        raise DomainError("not_found", "Registered portable evaluation not found", 404)
    return _complete_portable_evidence(
        session,
        run,
        attempt,
        config,
        row,
        outcome=outcome,
        sandbox_stopped=sandbox_stopped,
        settings=settings,
    )


def _complete_portable_evidence(
    session, run, attempt, config, row, *, outcome, sandbox_stopped, settings
):
    """Shared evidence verifier; callers must fence and admit their exact split first."""
    encoded = bounded_json(outcome)
    fingerprint = hashlib.sha256(encoded).hexdigest()
    if row.finished_at is not None:
        if row.payload_sha256 != fingerprint or row.sandbox_stopped != sandbox_stopped:
            raise DomainError(
                "immutable_evaluation", "Completed portable evaluation cannot change"
            )
        return row
    if not sandbox_stopped:
        row.error = "Portable sibling termination unconfirmed; recovery blocked"
        append_event(
            session,
            run,
            "recovery_blocked",
            {"evaluation_id": str(row.id), "message": row.error},
            attempt.id,
        )
        return row
    if type(outcome) is not dict:
        raise DomainError(
            "invalid_research_payload", "Portable outcome must be an object", 422
        )
    status = outcome.get("status", "failed")
    if status not in {"succeeded", "failed", "cancelled", "interrupted"}:
        status = "failed"
    error = outcome.get("error")
    if status == "succeeded":
        try:
            ref = outcome["portable_result"]
            session.flush()
            artifact = session.get(Artifact, UUID(ref["artifact_id"]))
            if (
                artifact is None
                or artifact.run_id != run.id
                or artifact.attempt_id != attempt.id
                or artifact.name != f"evaluation-{row.id}-portable-result.json"
                or artifact.media_type != "application/json"
                or artifact.sha256 != ref["sha256"]
                or artifact.size_bytes != ref["size_bytes"]
                or not 0 < artifact.size_bytes <= 8 * 1_048_576
                or outcome.get("image_digest") != run.research_image_digest
                or outcome.get("policy_image_digest")
                != run.portable_policy_image_digest
                or outcome.get("exit_code") != 0
            ):
                raise ValueError(
                    "Portable result lacks registered sibling-bound artifact evidence"
                )
            content = read_verified(
                settings.storage_root,
                artifact.storage_key,
                artifact.sha256,
                settings.max_artifact_bytes,
            )
            bundle = verify_portable_bundle_binding(content, config, row)
            if (
                outcome.get("metrics") != bundle["summary"]
                or outcome.get("combined_score") != portable_score(bundle)
                or outcome.get("result_trust") != bundle["result_trust"]
                or outcome.get("ledger_origin") != "trusted_engine"
                or outcome.get("runtime_certified") is not False
            ):
                raise ValueError(
                    "Portable summary differs from verified engine evidence"
                )
        except (KeyError, ValueError, TypeError, OSError, DomainError):
            status, error = (
                "failed",
                "Portable result evidence failed independent validation",
            )
    # Never leave an asserted success/trust label in failed durable evidence.
    stored = dict(outcome)
    stored["status"] = status
    if status != "succeeded":
        for key in (
            "result_trust",
            "ledger_origin",
            "portable_result",
            "combined_score",
            "metrics",
        ):
            stored.pop(key, None)
    if error:
        stored["error"] = redact(str(error))[:4000]
    encoded = bounded_json(stored)
    row.status, row.payload, row.payload_sha256 = (
        status,
        json.loads(encoded),
        hashlib.sha256(encoded).hexdigest(),
    )
    row.error, row.sandbox_stopped, row.finished_at = (
        stored.get("error"),
        True,
        utcnow(),
    )
    append_event(
        session,
        run,
        "evaluation_completed",
        {
            "evaluation_id": str(row.id),
            "candidate_key": row.candidate_key,
            "split": row.split,
            "status": status,
            "payload_sha256": row.payload_sha256,
        },
        attempt.id,
    )
    session.flush()
    return row


def pause_portable_run(session, run_id, owner_id):
    run = owned_portable_run(session, run_id, owner_id, lock=True)
    if run.status in {"pause_requested", "paused"}:
        return run
    if run.status not in {"queued", "preparing", "running"}:
        raise DomainError(
            "invalid_transition", "Only active portable research can pause"
        )
    run.status = "pause_requested"
    append_event(
        session, run, "stage_changed", {"status": run.status}, run.active_attempt_id
    )
    return run


def resume_portable_run(session, run_id, owner_id, settings):
    advisory_lock(session, f"submit:{owner_id}")
    run = owned_portable_run(session, run_id, owner_id, lock=True)
    if run.status not in {"paused", "failed", "cancelled", "interrupted"}:
        raise DomainError(
            "invalid_transition", "Only stopped portable research can resume"
        )
    if run.active_attempt_id:
        prior = session.get(ExecutionAttempt, run.active_attempt_id)
        if prior is None or not prior.sandbox_stopped:
            raise DomainError(
                "sandbox_not_stopped",
                "Previous sibling group must be confirmed stopped",
            )
    if session.scalar(
        select(Evaluation.id)
        .where(Evaluation.run_id == run.id, Evaluation.sandbox_stopped.is_(False))
        .limit(1)
    ):
        raise DomainError(
            "sandbox_not_stopped",
            "Every portable sibling group must be confirmed stopped",
        )
    config = portable_config(run)
    enforce_model_policy(session, owner_id, config.model)
    validate_model_selection(config.model, settings, owner_id)
    if run.active_attempt_id and config.model.provider != "fixture":
        from backend.app.model_containment import model_processes_confirmed_stopped

        if not model_processes_confirmed_stopped(session, run.active_attempt_id):
            raise DomainError(
                "model_process_not_stopped",
                "Every admitted model process needs a durable stop receipt before resume",
            )
    latest_checkpoint_for_resume(session, run, settings)
    active = session.scalar(
        select(func.count())
        .select_from(ResearchRun)
        .where(ResearchRun.owner_id == owner_id, ResearchRun.status.not_in(TERMINAL))
    )
    if active >= quota_limit(session, owner_id, settings):
        raise DomainError("run_quota", "Active run limit reached", 429)
    run.resume_checkpoint_id = run.latest_checkpoint_id
    run.generation += 1
    run.active_attempt_id = None
    run.status, run.error, run.result = "queued", None, None
    session.add(OutboxEvent(run_id=run.id, generation=run.generation))
    append_event(
        session,
        run,
        "stage_changed",
        {
            "status": "queued",
            "generation": run.generation,
            "resume_mode": "verified_checkpoint",
            "checkpoint_id": str(run.resume_checkpoint_id),
        },
    )
    return run
