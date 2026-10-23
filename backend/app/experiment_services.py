"""Owner-wide immutable split/selection/exposure transactions.

Callers own commit. Write lock order is owner submission -> experiment family ->
split -> run. Neither a refresh, renamed symbol, new family nor a cancelled final
admission removes evidence. No tracked overlap is never an unseen-data promise.
"""

from __future__ import annotations

import hashlib
from datetime import date
from uuid import UUID, uuid4

from sqlalchemy import or_, select

from backend.app.experiment_schemas import (
    ExperimentCreate,
    ExposureSnapshot,
    SplitDefinition,
    SplitVersionCreate,
)
from backend.app.models import (
    Candidate,
    DatasetVersion,
    DataUsageExposure,
    Evaluation,
    Experiment,
    FinalSelection,
    Listing,
    ResearchCheckpoint,
    SplitVersion,
    StrategyVersion,
    UniverseVersion,
    utcnow,
)
from backend.app.research_services import (
    _load_checkpoint,
    _reference,
    bounded_json,
    checkpoint_reference,
    execution_config,
)
from backend.app.services import DomainError, advisory_lock, owned_run
from packages.tradegrad_adapter.config import DateWindow

TRACKING_COVERAGE = "historical tracking incomplete/external use unknown"


def owned_resource(session, model, identifier, owner_id, *, lock=False):
    query = select(model).where(model.id == identifier, model.owner_id == owner_id)
    if lock:
        query = query.with_for_update()
    item = session.scalar(query)
    if item is None:
        raise DomainError("not_found", "Owned research resource not found", 404)
    return item


def lock_split(session, owner_id, split):
    """Also acquire the owner lock when called independently; transaction reentry is safe."""
    advisory_lock(session, f"submit:{owner_id}")
    experiment = owned_resource(session, Experiment, split.experiment_id, owner_id)
    advisory_lock(
        session, f"experiment:{experiment.root_experiment_id or experiment.id}"
    )
    advisory_lock(session, f"split:{split.id}")
    return experiment


def create_experiment(session, owner_id, body: ExperimentCreate):
    advisory_lock(session, f"submit:{owner_id}")
    parent = (
        owned_resource(session, Experiment, body.parent_experiment_id, owner_id)
        if body.parent_experiment_id
        else None
    )
    if parent:
        advisory_lock(session, f"experiment:{parent.root_experiment_id or parent.id}")
    item = Experiment(
        id=uuid4(),
        owner_id=owner_id,
        name=body.name.strip() or "Experiment",
        parent_experiment_id=parent.id if parent else None,
        root_experiment_id=(parent.root_experiment_id or parent.id) if parent else None,
    )
    session.add(item)
    session.flush()
    return item


def split_definition(split):
    try:
        definition = SplitDefinition.model_validate(split.definition)
        if definition.sha256 != split.content_sha256:
            raise ValueError("Split digest mismatch")
        if (
            UUID(definition.dataset.id) != split.dataset_version_id
            or UUID(definition.universe.id) != split.universe_version_id
        ):
            raise ValueError("Split reference identity mismatch")
        return definition
    except (ValueError, TypeError):
        raise DomainError(
            "split_invalid", "Immutable split definition or digest differs", 409
        ) from None


def validate_split_definition(session, owner_id, definition):
    dataset = _reference(session, DatasetVersion, definition.dataset, owner_id)
    universe = _reference(session, UniverseVersion, definition.universe, owner_id)
    for _, window in definition.windows.items():  # noqa: PERF102 (WindowSet is not a dict)
        if window.start < dataset.start_date or window.end > dataset.end_date:
            raise DomainError(
                "coverage_error", "Split lies outside pinned dataset coverage", 422
            )
    actual = {
        str(row.get("listing_id")) for row in dataset.manifest.get("listings", [])
    }
    expected = {str(row.get("listing_id")) for row in universe.members}
    if not actual or "None" in actual or actual != expected:
        raise DomainError(
            "universe_mismatch",
            "Split dataset and universe listings must match exactly",
            422,
        )
    pinned = dataset.manifest.get("universe", {})
    if pinned.get("id") and (
        pinned["id"] != str(universe.id)
        or pinned.get("content_sha256") != universe.content_sha256
    ):
        raise DomainError("universe_mismatch", "Dataset pins a different universe", 422)
    return dataset, universe


def create_split_version(session, owner_id, body: SplitVersionCreate):
    advisory_lock(session, f"submit:{owner_id}")
    experiment = owned_resource(session, Experiment, body.experiment_id, owner_id)
    advisory_lock(
        session, f"experiment:{experiment.root_experiment_id or experiment.id}"
    )
    dataset, universe = validate_split_definition(session, owner_id, body.definition)
    # New identical splits are deliberate versions, not a way to erase exposure.
    item = SplitVersion(
        id=uuid4(),
        owner_id=owner_id,
        experiment_id=experiment.id,
        dataset_version_id=dataset.id,
        universe_version_id=universe.id,
        definition=body.definition.model_dump(mode="json"),
        content_sha256=body.definition.sha256,
    )
    session.add(item)
    session.flush()
    return item


def bind_research_submission(
    session,
    owner_id,
    config,
    dataset,
    universe,
    *,
    experiment_id=None,
    split_version_id=None,
):
    """Bind at INSERT, without adding any field to tradegrad.run.v1 JSON or hash."""
    advisory_lock(session, f"submit:{owner_id}")
    definition = SplitDefinition(
        dataset=config.dataset, universe=config.universe, windows=config.windows
    )
    if dataset.id != UUID(config.dataset.id) or universe.id != UUID(config.universe.id):
        raise DomainError(
            "reference_mismatch", "Submission resources differ from split", 422
        )
    if split_version_id:
        split = owned_resource(session, SplitVersion, split_version_id, owner_id)
        experiment = lock_split(session, owner_id, split)
        if experiment_id and experiment.id != experiment_id:
            raise DomainError(
                "split_mismatch", "Split belongs to another experiment", 422
            )
        if split_definition(split) != definition:
            raise DomainError(
                "split_mismatch", "RunConfig differs from immutable split", 422
            )
        if session.scalar(
            select(FinalSelection.id).where(FinalSelection.split_version_id == split.id)
        ):
            raise DomainError(
                "split_frozen",
                "A frozen split cannot accept further optimization; create a new split",
                409,
            )
        return experiment, split
    experiment = (
        owned_resource(session, Experiment, experiment_id, owner_id)
        if experiment_id
        else create_experiment(
            session, owner_id, ExperimentCreate(name="Research experiment")
        )
    )
    split = create_split_version(
        session,
        owner_id,
        SplitVersionCreate(experiment_id=experiment.id, definition=definition),
    )
    return experiment, split


def dataset_identities(dataset, universe=None, listing_symbols=None):
    """Stable listing identity plus deliberately conservative market/symbol fallback."""
    listing_symbols = listing_symbols or {}
    members = {
        str(row.get("listing_id")): row
        for row in (universe.members if universe else [])
    }
    mapping = dataset.manifest.get("evaluator_symbol_mapping", {})
    rows = dataset.manifest.get("listings", [])
    identities = []
    for row in rows:
        listing_id = str(row["listing_id"]) if row.get("listing_id") else None
        member = members.get(listing_id, {})
        symbol = (
            row.get("symbol")
            or member.get("symbol")
            or listing_symbols.get(listing_id)
            or mapping.get(listing_id)
        )
        identities.append(
            {
                "listing_id": listing_id,
                "market": dataset.market.upper(),
                "symbol": str(symbol).strip().upper() if symbol else None,
            }
        )
    if not identities:
        # Unknown historical identity is wildcard evidence, never a clean reset.
        identities = [
            {"listing_id": None, "market": dataset.market.upper(), "symbol": None}
        ]
    return sorted(
        {
            (row["listing_id"], row["market"], row["symbol"]): row for row in identities
        }.values(),
        key=lambda row: (row["listing_id"] or "", row["market"], row["symbol"] or ""),
    )


def _identities(session, dataset, universe=None):
    identifiers = []
    for row in dataset.manifest.get("listings", []):
        try:
            identifiers.append(UUID(str(row.get("listing_id"))))
        except (TypeError, ValueError):
            pass
    symbols = (
        {
            str(row.id): row.symbol
            for row in session.scalars(
                select(Listing).where(Listing.id.in_(identifiers))
            ).all()
        }
        if identifiers
        else {}
    )
    return dataset_identities(dataset, universe, symbols)


def identities_overlap(first, second):
    for left in first:
        for right in second:
            if left.get("listing_id") and left["listing_id"] == right.get("listing_id"):
                return True
            if left.get("market") == right.get("market"):
                if (
                    left.get("symbol")
                    and right.get("symbol")
                    and left["symbol"].upper() == right["symbol"].upper()
                ):
                    return True
                if (not left.get("listing_id") and not left.get("symbol")) or (
                    not right.get("listing_id") and not right.get("symbol")
                ):
                    return True
    return False


def classify_exposures(identities, window, exposures, *, as_of=None):
    matches = [
        row
        for row in exposures
        if row.start_date < window.end
        and window.start < row.end_date
        and identities_overlap(identities, row.identities)
    ]
    unknown = not identities or any(
        not row.get("listing_id") and not row.get("symbol") for row in identities
    )
    return ExposureSnapshot(
        classification="previously_exposed"
        if matches
        else ("unknown_identity" if unknown else "no_tracked_overlap"),
        matching_exposure_ids=[row.id for row in matches[:1000]],
        matching_purposes=sorted({row.purpose for row in matches}),
        matching_count=len(matches),
        evidence_truncated=len(matches) > 1000,
        identities=identities,
        as_of=as_of or utcnow(),
    ).model_dump(mode="json")


def get_exposure_snapshot(session, owner_id, split, *, exclude_run_id=None):
    if split.owner_id != owner_id:
        raise DomainError("not_found", "Split not found", 404)
    definition = split_definition(split)
    dataset, universe = validate_split_definition(session, owner_id, definition)
    window = definition.windows.holdout
    if window is None:
        raise DomainError(
            "holdout_required", "Final selection requires a configured holdout", 422
        )
    query = (
        select(DataUsageExposure)
        .where(
            DataUsageExposure.owner_id == owner_id,
            DataUsageExposure.start_date < window.end,
            DataUsageExposure.end_date > window.start,
        )
        .order_by(DataUsageExposure.created_at, DataUsageExposure.id)
    )
    if exclude_run_id:
        query = query.where(
            or_(
                DataUsageExposure.run_id.is_(None),
                DataUsageExposure.run_id != exclude_run_id,
            )
        )
    exposures = session.scalars(query).all()
    return classify_exposures(
        _identities(session, dataset, universe), window, exposures
    )


def record_exposure(
    session,
    *,
    owner_id,
    dataset,
    window,
    purpose,
    source_key,
    provenance,
    run=None,
    universe=None,
):
    advisory_lock(session, f"submit:{owner_id}")
    if dataset.owner_id != owner_id or (run and run.owner_id != owner_id):
        raise DomainError("not_found", "Exposure resource not found", 404)
    identities = _identities(session, dataset, universe)
    bounded_json(provenance)
    previous = session.scalar(
        select(DataUsageExposure).where(
            DataUsageExposure.owner_id == owner_id,
            DataUsageExposure.source_key == source_key,
        )
    )
    if previous:
        if (
            previous.dataset_version_id != dataset.id
            or previous.start_date != window.start
            or previous.end_date != window.end
            or previous.purpose != purpose
            or previous.identities != identities
            or previous.provenance != provenance
        ):
            raise DomainError(
                "exposure_conflict",
                "An exposure admission identity cannot be reused with different evidence",
            )
        return previous
    item = DataUsageExposure(
        id=uuid4(),
        owner_id=owner_id,
        dataset_version_id=dataset.id,
        experiment_id=run.experiment_id if run else None,
        split_version_id=run.split_version_id if run else None,
        run_id=run.id if run else None,
        source_key=source_key,
        identities=identities,
        start_date=window.start,
        end_date=window.end,
        purpose=purpose,
        provenance=provenance,
    )
    session.add(item)
    session.flush()
    return item


def record_research_exposure(session, run, dataset, universe, config):
    """Conservatively include all possible prior warmup bars; never holdout futures."""
    for split_name, window in config.windows.optimization_windows():
        record_exposure(
            session,
            owner_id=run.owner_id,
            dataset=dataset,
            universe=universe,
            run=run,
            window=DateWindow(start=dataset.start_date, end=window.end),
            purpose=f"optimization_{split_name}_admission",
            source_key=f"research:{run.id}:{split_name}",
            provenance={
                "requested_window": window.model_dump(mode="json"),
                "config_sha256": config.sha256,
                "warmup_coverage": "conservative_dataset_prefix",
                "admission_before_dispatch": True,
            },
        )


def record_legacy_run_exposure(session, run, dataset):
    window = DateWindow(
        start=dataset.start_date, end=date.fromisoformat(run.config["end_date"])
    )
    return record_exposure(
        session,
        owner_id=run.owner_id,
        dataset=dataset,
        window=window,
        run=run,
        purpose="seed_evaluation_admission",
        source_key=f"legacy-run:{run.id}",
        provenance={
            "requested_start": run.config["start_date"],
            "coverage": "conservative_dataset_prefix",
            "admission_before_dispatch": True,
        },
    )


def record_dataset_access(
    session, owner_id, dataset, *, purpose, source_key, window=None
):
    if purpose not in {
        "dataset_preview",
        "dataset_download",
        "result_preview",
        "result_download",
    }:
        raise DomainError(
            "invalid_exposure_purpose", "Unsupported data access purpose", 422
        )
    return record_exposure(
        session,
        owner_id=owner_id,
        dataset=dataset,
        window=window or DateWindow(start=dataset.start_date, end=dataset.end_date),
        purpose=purpose,
        source_key=source_key,
        provenance={
            "coverage": "explicit_access_conservative",
            "tracking_coverage": TRACKING_COVERAGE,
        },
    )


def validate_selection_source(session, owner_id, body, settings):
    split = owned_resource(session, SplitVersion, body.split_version_id, owner_id)
    definition = split_definition(split)
    if definition.windows.holdout is None:
        raise DomainError(
            "holdout_required", "A final selection needs a pinned holdout window", 422
        )
    run = owned_run(session, body.source_run_id, owner_id)
    config = execution_config(run)
    if config.sha256 != run.request_sha256:
        raise DomainError("selection_hash_mismatch", "Source config digest differs")
    if (
        run.status != "succeeded"
        or config.execution_mode != "optimization"
        or run.experiment_id != split.experiment_id
        or run.split_version_id != split.id
    ):
        raise DomainError(
            "selection_source_invalid",
            "Source must be a succeeded optimization bound to this exact split",
        )
    if (
        SplitDefinition(
            dataset=config.dataset, universe=config.universe, windows=config.windows
        )
        != definition
    ):
        raise DomainError("selection_source_invalid", "Source config and split differ")
    candidate = session.scalar(
        select(Candidate).where(
            Candidate.id == body.source_candidate_id, Candidate.run_id == run.id
        )
    )
    checkpoint = session.scalar(
        select(ResearchCheckpoint).where(
            ResearchCheckpoint.id == body.source_checkpoint_id,
            ResearchCheckpoint.run_id == run.id,
            ResearchCheckpoint.complete.is_(True),
        )
    )
    if candidate is None or checkpoint is None:
        raise DomainError(
            "not_found", "Candidate or published checkpoint not found", 404
        )
    if (
        checkpoint.config_sha256 != config.sha256
        or hashlib.sha256(candidate.code.encode()).hexdigest() != candidate.code_sha256
    ):
        raise DomainError(
            "selection_hash_mismatch",
            "Candidate code or checkpoint config hash differs",
        )
    _, _, state, _ = _load_checkpoint(
        checkpoint_reference(checkpoint, settings),
        config,
        settings,
        run_id=run.id,
        attempt_id=checkpoint.attempt_id,
    )
    summary = checkpoint.summary.get("candidates", {}).get(candidate.candidate_key, {})
    row = next(
        (
            row
            for row in state["candidates"]
            if row["candidate_id"] == candidate.candidate_key
        ),
        None,
    )
    if (
        not row
        or row.get("code") != candidate.code
        or row.get("code_sha256") != candidate.code_sha256
        or not row.get("valid")
        or summary.get("id") != str(candidate.id)
        or not summary.get("valid")
        or not summary.get("complete")
    ):
        raise DomainError(
            "candidate_ineligible",
            "Selected candidate is not valid and complete in that verified checkpoint",
        )
    evidence_ids = summary.get("evaluation_ids", [])
    physical = session.scalars(
        select(Evaluation).where(
            Evaluation.run_id == run.id,
            Evaluation.candidate_id == candidate.id,
            Evaluation.id.in_([UUID(value) for value in evidence_ids]),
        )
    ).all()
    if (
        len(physical) != len(evidence_ids)
        or not physical
        or any(
            item.status != "succeeded"
            or not item.sandbox_stopped
            or item.code_sha256 != candidate.code_sha256
            for item in physical
        )
    ):
        raise DomainError(
            "candidate_ineligible",
            "Selected candidate evaluation evidence is incomplete",
        )
    if config.schema_version == "tradegrad.portable.run.v1":
        import re

        source_strategy = owned_resource(
            session, StrategyVersion, run.strategy_version_id, owner_id
        )
        portable_selected_metadata(config, source_strategy, candidate)
        if (
            not re.fullmatch(r"sha256:[0-9a-f]{64}", run.research_image_digest or "")
            or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", run.portable_policy_image_digest or ""
            )
            or any(
                item.image_digest != run.research_image_digest
                or (item.payload or {}).get("policy_image_digest")
                != run.portable_policy_image_digest
                or (item.payload or {}).get("result_trust")
                != "isolated_policy_trusted_akquant_engine"
                for item in physical
            )
        ):
            raise DomainError(
                "candidate_ineligible",
                "Portable selection requires exact dual-image successful evidence",
            )
    validate_split_definition(session, owner_id, definition)
    return split, run, config, candidate, checkpoint, summary


def portable_selected_metadata(config, source_strategy, candidate):
    """Derive only the selected code checksum; preserve stored policy semantics."""
    from packages.strategy_sdk import canonical_json, parse_json, parse_manifest
    from packages.tradegrad_adapter.portable_prompts import validate_policy_source
    from workers.research.portable_staging import validate_portable_binding

    try:
        if (
            source_strategy.mode != "portable"
            or str(source_strategy.id) != config.seed_strategy.id
            or source_strategy.content_sha256 != config.seed_strategy.sha256
            or hashlib.sha256(source_strategy.code.encode()).hexdigest()
            != config.seed_strategy.sha256
        ):
            raise ValueError("Source portable strategy changed")
        manifest = validate_portable_binding(
            config, source_strategy.manifest, source_strategy.parameters
        )
        validate_policy_source(candidate.code, config.strategy_kind)
        value = manifest.model_dump(mode="json")
        value["code_sha256"] = candidate.code_sha256
        value["reference_result_sha256"] = None
        return parse_manifest(canonical_json(value)).model_dump(
            mode="json"
        ), parse_json(canonical_json(source_strategy.parameters))
    except (ValueError, TypeError, KeyError):
        raise DomainError(
            "selection_source_invalid",
            "Stored portable policy contract or parameters differ",
        ) from None


def freeze_final_selection(session, owner_id, body, settings):
    advisory_lock(session, f"submit:{owner_id}")
    split = owned_resource(session, SplitVersion, body.split_version_id, owner_id)
    lock_split(session, owner_id, split)
    previous = session.scalar(
        select(FinalSelection).where(FinalSelection.split_version_id == split.id)
    )
    if previous:
        if previous.owner_id != owner_id or previous.request_sha256 != body.sha256:
            raise DomainError(
                "selection_frozen",
                "This split already has a different final selection; use a new split",
            )
        return previous
    split, run, config, candidate, checkpoint, summary = validate_selection_source(
        session, owner_id, body, settings
    )
    snapshot = get_exposure_snapshot(session, owner_id, split)
    portable = config.schema_version == "tradegrad.portable.run.v1"
    policy_manifest, parameters = (None, None)
    if portable:
        source_strategy = owned_resource(
            session, StrategyVersion, run.strategy_version_id, owner_id
        )
        policy_manifest, parameters = portable_selected_metadata(
            config, source_strategy, candidate
        )
    strategy = StrategyVersion(
        id=uuid4(),
        owner_id=owner_id,
        name=f"Frozen selection {candidate.candidate_key}"[:255],
        task=config.task,
        mode="portable" if portable else "native_akquant",
        manifest=policy_manifest,
        parameters=parameters,
        code=candidate.code,
        content_sha256=candidate.code_sha256,
        parent_id=run.strategy_version_id,
    )
    session.add(strategy)
    session.flush()
    selection = FinalSelection(
        id=uuid4(),
        owner_id=owner_id,
        experiment_id=split.experiment_id,
        split_version_id=split.id,
        source_run_id=run.id,
        source_candidate_id=candidate.id,
        source_checkpoint_id=checkpoint.id,
        strategy_version_id=strategy.id,
        request_sha256=body.sha256,
        source_config_sha256=config.sha256,
        source_code_sha256=candidate.code_sha256,
        source_checkpoint_sha256=checkpoint.manifest_sha256,
        selection_evidence={
            "candidate_key": candidate.candidate_key,
            "chosen_candidate_id": str(candidate.id),
            "automatic_best_candidate_id": (run.result or {}).get("best_candidate_id"),
            "checkpoint_sequence": checkpoint.sequence,
            "checkpoint_input_digest": checkpoint.input_digest,
            "evaluation_ids": summary["evaluation_ids"],
            "combined_score": summary.get("combined_score"),
            "valid": True,
            "complete": True,
            "source_config": config.model_dump(mode="json"),
        },
        exposure_snapshot=snapshot,
    )
    session.add(selection)
    session.flush()
    return selection
