"""Append-only owner-scoped exports of verified final-selection evidence.

A package is not an execution attempt. Files publish no-clobber before the DB row
commits; a rollback may leave only an unreferenced content-addressed object.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.atomic_storage import ContentConflict, publish_bytes
from backend.app.experiment_services import record_exposure
from backend.app.models import (
    Artifact,
    FinalSelection,
    ResearchRun,
    SplitVersion,
    StrategyPackage,
    StrategyVersion,
    UniverseVersion,
)
from backend.app.services import DomainError, advisory_lock
from backend.app.storage import read_verified, safe_path
from backend.app.strategy_package_schemas import StrategyPackageCreate
from packages.tradegrad_adapter.config import DateWindow
from packages.tradegrad_adapter.strategy_package import (
    ALLOWED_LOCKS,
    FIXED_PATHS,
    LOCK_EXPORT_PREFIX,
    MAX_ENTRIES,
    MAX_ENTRY_BYTES,
    MAX_ENVIRONMENT_BYTES,
    MAX_TOTAL_BYTES,
    NATIVE_ENV_SCHEMA,
    PORTABLE_ENV_SCHEMA,
    PackageError,
    PackageInput,
    build_strategy_package,
    canonical,
    capture_environment_evidence,
    parse_canonical,
    safe_relative_path,
    sha256,
    validate_environment_evidence,
)

PACKAGE_NAMESPACE = UUID("c3e71269-287a-450e-8fea-195e8822ebcf")


def _invalid(message="Frozen package evidence is missing or inconsistent"):
    return DomainError("invalid_package_evidence", message, 409)


def package_request(selection_id: UUID, body: StrategyPackageCreate) -> dict:
    return {
        "schema_version": "tradegrad.package-request.v1",
        "final_selection_id": str(selection_id),
        **body.model_dump(mode="json"),
    }


def package_id(owner_id: UUID, request: dict) -> UUID:
    return uuid5(PACKAGE_NAMESPACE, f"{owner_id}:{sha256(canonical(request))}")


def get_owned_package(session: Session, owner_id: UUID, identifier: UUID):
    item = session.scalar(
        select(StrategyPackage).where(
            StrategyPackage.id == identifier, StrategyPackage.owner_id == owner_id
        )
    )
    if item is None or item.owner_id != owner_id:
        raise DomainError("not_found", "Strategy package not found", 404)
    return item


def read_package(item, settings) -> bytes:
    """Integrity-only primitive; user-facing reads must use read_owned_package."""
    try:
        safe_relative_path(item.storage_key)
    except PackageError:
        raise _invalid("Stored package path is invalid") from None
    if not 0 < item.size_bytes <= MAX_TOTAL_BYTES:
        raise _invalid("Stored package exceeds its immutable size limit")
    content = read_verified(
        settings.storage_root, item.storage_key, item.archive_sha256, MAX_TOTAL_BYTES
    )
    if len(content) != item.size_bytes:
        raise _invalid("Package size does not match immutable registration")
    return content


def _verify_package_reference(item, content, selection, verified):
    """Bind immutable ZIP registration and its exact reference to current evidence.

    No extraction or strategy execution is performed. Read only the bounded,
    uncompressed inventory produced by our deterministic package writer.
    """
    try:
        manifest = item.manifest
        if (
            manifest["schema_version"] != "tradegrad.strategy-package.v1"
            or manifest["profile"] != "reference_only.v1"
            or manifest["raw_market_data_included"] is not False
            or manifest["content_sha256"] != item.content_sha256
            or sha256(canonical(manifest["files"])) != item.content_sha256
        ):
            raise ValueError("Package registration differs from inventory")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (
                not 0 < len(entries) <= MAX_ENTRIES
                or len(names) != len(set(names))
                or set(names) != {*manifest["files"], "package-manifest.json"}
                or sum(entry.file_size for entry in entries) > MAX_TOTAL_BYTES
            ):
                raise ValueError("Package inventory differs")
            files = {}
            for entry in entries:
                name = safe_relative_path(entry.filename)
                if (
                    name not in FIXED_PATHS
                    and name
                    not in {LOCK_EXPORT_PREFIX + path for path in ALLOWED_LOCKS}
                ) or (
                    entry.compress_type != zipfile.ZIP_STORED
                    or entry.flag_bits & 1
                    or not 0 <= entry.file_size <= MAX_ENTRY_BYTES
                ):
                    raise ValueError("Unsupported package entry")
                value = archive.read(entry)
                if name == "package-manifest.json":
                    if value != canonical(manifest):
                        raise ValueError("Archive manifest differs from registration")
                elif manifest["files"][name] != {
                    "sha256": sha256(value),
                    "bytes": len(value),
                }:
                    raise ValueError("Archive entry differs from registration")
                files[name] = value
        frozen = parse_canonical(files["selection/frozen-selection.json"])
        run, evaluation = verified.run, verified.evaluation
        expected = {
            "schema_version": "tradegrad.frozen-selection-export.v1",
            "selection_id": str(selection.id),
            "experiment_id": str(run.experiment_id),
            "split_version_id": str(run.split_version_id),
            "split_sha256": run.config["split_sha256"],
            "strategy_version_id": str(run.strategy_version_id),
            "source_run_id": str(selection.source_run_id),
            "source_candidate_id": str(selection.source_candidate_id),
            "source_checkpoint_id": str(selection.source_checkpoint_id),
            "source_checkpoint_sha256": selection.source_checkpoint_sha256,
            "selection_request_sha256": selection.request_sha256,
            "selection_evidence_sha256": sha256(
                canonical(selection.selection_evidence)
            ),
            "source_code_sha256": selection.source_code_sha256,
            "source_config_sha256": selection.source_config_sha256,
            "final_run_id": str(run.id),
            "final_config_sha256": run.request_sha256,
            "reference_evaluation_id": str(evaluation.id),
            "reference_result_sha256": verified.artifact.sha256,
            "dataset_sha256": verified.dataset.content_sha256,
            "universe_sha256": run.config["source_config"]["universe"]["sha256"],
        }
        if (
            any(frozen.get(key) != value for key, value in expected.items())
            or files["reference/result-bundle.json"] != verified.content
        ):
            raise ValueError("Package reference differs from current final evidence")
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        zipfile.BadZipFile,
    ):
        raise _invalid("Immutable package reference or registration differs") from None


def _record_package_export(session, owner_id, item, verified):
    record_exposure(
        session,
        owner_id=owner_id,
        dataset=verified.dataset,
        run=verified.run,
        window=DateWindow(**verified.run.config["holdout"]),
        purpose="strategy_package_export",
        source_key=f"strategy-package:{item.id}:export",
        provenance={
            "package_id": str(item.id),
            "request_sha256": item.request_sha256,
            "archive_sha256": item.archive_sha256,
            "selection_id": str(item.final_selection_id),
            "evaluation_id": str(verified.evaluation.id),
            "final_config_sha256": verified.run.request_sha256,
            "artifact_sha256": verified.artifact.sha256,
        },
    )


def read_owned_package(session, owner_id, item, settings, *, selection=None):
    """Current-final gates apply even to a byte-identical historical archive.

    Callers must commit the export exposure before releasing any response body.
    Metadata-only detail may remain readable when current evidence is unavailable.
    """
    if item.owner_id != owner_id:
        raise DomainError("not_found", "Strategy package not found", 404)
    try:
        body = StrategyPackageCreate.model_validate(
            {
                "reference_evaluation_id": item.request["reference_evaluation_id"],
                "profile": item.request["profile"],
            }
        )
        request = package_request(item.final_selection_id, body)
        if (
            item.request != request
            or item.request_sha256 != sha256(canonical(request))
            or item.id != package_id(owner_id, request)
        ):
            raise ValueError("Package request identity differs")
    except (AttributeError, KeyError, TypeError, ValueError):
        raise _invalid("Immutable package request registration differs") from None
    if selection is None:
        selection = session.scalar(
            select(FinalSelection).where(
                FinalSelection.id == item.final_selection_id,
                FinalSelection.owner_id == owner_id,
            )
        )
    if (
        selection is None
        or selection.owner_id != owner_id
        or selection.id != item.final_selection_id
    ):
        raise DomainError("not_found", "Final selection not found", 404)
    source = session.scalar(
        select(ResearchRun).where(
            ResearchRun.id == selection.source_run_id, ResearchRun.owner_id == owner_id
        )
    )
    if source is None or source.owner_id != owner_id:
        raise _invalid()
    if source.config.get("schema_version") == "tradegrad.portable.run.v1":
        from backend.app.portable_final_views import load_owned_final_result
    elif source.config.get("schema_version") == "tradegrad.run.v1":
        from backend.app.final_evaluation_views import load_owned_final_result
    else:
        raise _invalid()
    verified = load_owned_final_result(
        session, body.reference_evaluation_id, owner_id, settings
    )
    if (
        verified.run.owner_id != owner_id
        or verified.evaluation.id != body.reference_evaluation_id
        or verified.run.final_selection_id != selection.id
    ):
        raise _invalid()
    content = read_package(item, settings)
    _verify_package_reference(item, content, selection, verified)
    _record_package_export(session, owner_id, item, verified)
    return content


def load_environment_evidence(session, verified, settings):
    artifact = session.scalar(
        select(Artifact).where(
            Artifact.run_id == verified.run.id,
            Artifact.attempt_id == verified.evaluation.attempt_id,
            Artifact.name == "execution-environment.json",
        )
    )
    config = verified.run.config
    if artifact is not None:
        if (
            artifact.media_type != "application/json"
            or not 0 < artifact.size_bytes <= MAX_ENVIRONMENT_BYTES
        ):
            raise _invalid("Invalid execution-time environment evidence")
        safe_relative_path(artifact.storage_key)
        content = read_verified(
            settings.storage_root,
            artifact.storage_key,
            artifact.sha256,
            MAX_ENVIRONMENT_BYTES,
        )
        if len(content) != artifact.size_bytes:
            raise _invalid("Execution evidence size mismatch")
        evidence = parse_canonical(content)
    else:
        if config.get("schema_version") == "tradegrad.portable.final-evaluation.v1":
            raise _invalid(
                "Portable packages require captured dual-image execution evidence"
            )
        # Never substitute current locks into a historical execution. The helper
        # refuses to reconstruct unless every reviewed input still matches.
        evidence = capture_environment_evidence(
            config["environment_digest"], verified.evaluation.image_digest
        )
        evidence["provenance"] = "reconstructed_exact_digest"
    portable = config.get("schema_version") == "tradegrad.portable.final-evaluation.v1"
    expected_schema = PORTABLE_ENV_SCHEMA if portable else NATIVE_ENV_SCHEMA
    if evidence.get("schema_version") != expected_schema:
        raise _invalid("Result family and environment evidence differ")
    images = (
        {
            "engine": verified.run.research_image_digest,
            "policy": verified.run.portable_policy_image_digest,
        }
        if portable
        else verified.evaluation.image_digest
    )
    if portable and evidence.get("provenance") != "captured_at_execution":
        raise _invalid("Portable evidence must be captured at execution")
    validate_environment_evidence(evidence, config["environment_digest"], images)
    return evidence


def _universe_manifest(universe):
    """Preserve exact new definitions; never pretend a legacy projection is original."""
    definition = getattr(universe, "definition", None)
    if definition is not None:
        if (
            not isinstance(definition, dict)
            or sha256(canonical(definition)) != universe.content_sha256
        ):
            raise _invalid("Preserved universe definition differs from frozen checksum")
        representation, status = definition, "original_canonical_json"
    else:
        representation = {
            "name": universe.name,
            "state": universe.state,
            "as_of": universe.as_of.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "members": universe.members,
            "evidence": universe.evidence,
        }
        status = "serialization_not_original"
    return {
        "schema_version": "tradegrad.universe-export.v1",
        "id": str(universe.id),
        "reference_sha256": universe.content_sha256,
        "serialization": status,
        "manifest": representation,
        "manifest_sha256": sha256(canonical(representation)),
        "replay_dependency": "Matching verified dataset snapshot and evaluator mapping are required",
    }


def create_strategy_package(
    session: Session,
    owner_id: UUID,
    selection_id: UUID,
    body: StrategyPackageCreate,
    settings,
) -> tuple[StrategyPackage, bool]:
    # Owner authorization precedes both artifact lookup and all filesystem reads.
    selection = session.scalar(
        select(FinalSelection).where(
            FinalSelection.id == selection_id, FinalSelection.owner_id == owner_id
        )
    )
    if selection is None or selection.owner_id != owner_id:
        raise DomainError("not_found", "Final selection not found", 404)
    request = package_request(selection_id, body)
    request_hash = sha256(canonical(request))
    identifier = package_id(owner_id, request)
    advisory_lock(session, f"strategy-package:{owner_id}:{request_hash}")
    existing = session.scalar(
        select(StrategyPackage).where(
            StrategyPackage.id == identifier, StrategyPackage.owner_id == owner_id
        )
    )
    if existing is not None:
        if (
            existing.request != request
            or existing.request_sha256 != request_hash
            or existing.final_selection_id != selection_id
        ):
            raise DomainError(
                "idempotency_conflict",
                "Package identity already has different immutable input",
                409,
            )
        read_owned_package(session, owner_id, existing, settings, selection=selection)
        return existing, False
    source_run = session.scalar(
        select(ResearchRun).where(
            ResearchRun.id == selection.source_run_id, ResearchRun.owner_id == owner_id
        )
    )
    if source_run is None:
        raise _invalid()
    portable = source_run.config.get("schema_version") == "tradegrad.portable.run.v1"
    if portable:
        from backend.app.portable_final_views import load_owned_final_result
    else:
        from backend.app.final_evaluation_views import load_owned_final_result
    verified = load_owned_final_result(
        session, body.reference_evaluation_id, owner_id, settings
    )
    run, evaluation = verified.run, verified.evaluation
    strategy = session.scalar(
        select(StrategyVersion).where(
            StrategyVersion.id == selection.strategy_version_id,
            StrategyVersion.owner_id == owner_id,
        )
    )
    split = session.scalar(
        select(SplitVersion).where(
            SplitVersion.id == selection.split_version_id,
            SplitVersion.owner_id == owner_id,
        )
    )
    if (
        source_run is None
        or strategy is None
        or split is None
        or run.final_selection_id != selection.id
        or run.split_version_id != split.id
        or run.strategy_version_id != strategy.id
        or run.experiment_id != selection.experiment_id
        or evaluation.split != "holdout"
        or evaluation.code_sha256 != selection.source_code_sha256
        or strategy.content_sha256 != selection.source_code_sha256
        or run.config.get("schema_version")
        != (
            "tradegrad.portable.final-evaluation.v1"
            if portable
            else "tradegrad.final-evaluation.v1"
        )
        or run.config.get("selection_id") != str(selection.id)
        or run.config.get("source_config_sha256") != selection.source_config_sha256
        or run.config.get("source_config") != source_run.config
        or run.request_sha256 != sha256(canonical(run.config))
        or source_run.request_sha256 != selection.source_config_sha256
        or sha256(canonical(source_run.config)) != selection.source_config_sha256
        or split.content_sha256 != sha256(canonical(split.definition))
    ):
        raise _invalid()
    universe = session.scalar(
        select(UniverseVersion).where(
            UniverseVersion.id == split.universe_version_id,
            UniverseVersion.owner_id == owner_id,
        )
    )
    if (
        universe is None
        or source_run.config.get("universe", {}).get("sha256")
        != universe.content_sha256
    ):
        raise _invalid("Frozen universe reference does not match")
    universe_manifest = _universe_manifest(universe)
    try:
        from backend.app.experiment_services import get_exposure_snapshot

        # Serialize against owner exposure admissions, then retain this exact as-of.
        advisory_lock(session, f"submit:{owner_id}")
        snapshot = get_exposure_snapshot(session, owner_id, split)
        as_of = datetime.fromisoformat(snapshot["as_of"])
        # Read the immutable manifest only, never arbitrary data files or folders.
        manifest_key = f"{verified.dataset.storage_key}/manifest.json"
        safe_relative_path(manifest_key)
        manifest_bytes = read_verified(
            settings.storage_root,
            manifest_key,
            verified.dataset.content_sha256,
            8 * 1024 * 1024,
        )
        if manifest_bytes != canonical(verified.dataset.manifest):
            raise _invalid("Dataset manifest differs from persisted exact bytes")
        evidence = load_environment_evidence(session, verified, settings)
        frozen = {
            "schema_version": "tradegrad.frozen-selection-export.v1",
            "selection_id": str(selection.id),
            "experiment_id": str(selection.experiment_id),
            "split_version_id": str(split.id),
            "split_sha256": split.content_sha256,
            "strategy_version_id": str(strategy.id),
            "source_run_id": str(source_run.id),
            "source_candidate_id": str(selection.source_candidate_id),
            "source_checkpoint_id": str(selection.source_checkpoint_id),
            "source_checkpoint_sha256": selection.source_checkpoint_sha256,
            "selection_request_sha256": selection.request_sha256,
            "selection_evidence_sha256": sha256(
                canonical(selection.selection_evidence)
            ),
            "selection_evidence": {
                key: selection.selection_evidence[key]
                for key in (
                    "candidate_key",
                    "chosen_candidate_id",
                    "automatic_best_candidate_id",
                    "checkpoint_sequence",
                    "checkpoint_input_digest",
                    "evaluation_ids",
                    "combined_score",
                    "valid",
                    "complete",
                )
                if key in selection.selection_evidence
            },
            "selection_exposure_snapshot": selection.exposure_snapshot,
            "source_code_sha256": selection.source_code_sha256,
            "source_config_sha256": selection.source_config_sha256,
            "final_run_id": str(run.id),
            "final_config_sha256": run.request_sha256,
            "reference_evaluation_id": str(evaluation.id),
            "reference_result_sha256": verified.artifact.sha256,
            "selected_at": selection.created_at.isoformat(),
            "dataset_sha256": verified.dataset.content_sha256,
            "universe_sha256": universe.content_sha256,
        }
        built = build_strategy_package(
            PackageInput(
                strategy_code=strategy.code.encode("utf-8"),
                strategy_mode=strategy.mode,
                strategy_sha256=strategy.content_sha256,
                parameters=strategy.parameters if portable else None,
                policy_manifest=strategy.manifest if portable else None,
                selection=frozen,
                source_config=source_run.config,
                final_config=run.config,
                dataset_manifest=verified.dataset.manifest,
                universe_manifest=universe_manifest,
                split_version=split.definition,
                environment_evidence=evidence,
                result_content=verified.content,
                result_sha256=verified.artifact.sha256,
                exposure_snapshot=snapshot,
                exposure_as_of=as_of.isoformat(),
            )
        )
        key = f"strategy-packages/{owner_id}/{identifier}/{built.archive_sha256}.zip"
        target = safe_path(settings.storage_root, key)
        publish_bytes(
            target,
            built.archive,
            sha256=built.archive_sha256,
            durable_root=settings.storage_root,
        )
    except (PackageError, ContentConflict) as exc:
        raise _invalid(str(exc)) from None
    item = StrategyPackage(
        id=identifier,
        owner_id=owner_id,
        final_selection_id=selection_id,
        request_sha256=request_hash,
        request=request,
        content_sha256=built.content_sha256,
        archive_sha256=built.archive_sha256,
        size_bytes=len(built.archive),
        storage_key=key,
        manifest=built.manifest,
        exposure_snapshot=snapshot,
        exposure_as_of=as_of,
    )
    _verify_package_reference(item, built.archive, selection, verified)
    session.add(item)
    session.flush()
    _record_package_export(session, owner_id, item, verified)
    return item, True
