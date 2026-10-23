"""Bounded projections of independently verified, persisted portable evidence."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from backend.app.models import (
    Artifact,
    DatasetVersion,
    Evaluation,
    ModelCall,
    ResearchRun,
)
from backend.app.portable_schemas import PortablePreflightPublic, PortableResultQuery
from backend.app.portable_services import (
    owned_portable_run,
    portable_config,
    portable_score,
    validate_portable_submission,
    verify_portable_bundle_binding,
)
from backend.app.research_view_schemas import ResearchUsagePublic
from backend.app.services import DomainError
from backend.app.storage import read_verified
from packages.tradegrad_adapter.portable_config import portable_evaluation_windows


def preflight_portable(session, owner_id, body, settings):
    dataset, _, _, _ = validate_portable_submission(session, owner_id, body, settings)
    return PortablePreflightPublic(
        config_sha256=body.sha256,
        evaluation_windows=list(portable_evaluation_windows(body)),
        is_synthetic=dataset.is_synthetic,
        warnings=[
            "New portable research protocol; not upstream paper equivalence.",
            "Only explicit daily cash long-only semantics; runtime and external framework equivalence are not certified.",
        ]
        + (
            ["Synthetic data is for engineering validation, not investment evidence."]
            if dataset.is_synthetic
            else []
        ),
    )


@dataclass(frozen=True)
class OwnedPortableResult:
    evaluation: Evaluation
    artifact: Artifact
    bundle: dict
    content: bytes
    run: ResearchRun
    is_synthetic: bool


def load_owned_portable_result(session, evaluation_id, owner_id, settings):
    row = session.scalar(
        select(Evaluation)
        .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
        .where(Evaluation.id == evaluation_id, ResearchRun.owner_id == owner_id)
    )
    if row is None:
        raise DomainError("not_found", "Portable evaluation not found", 404)
    run = owned_portable_run(session, row.run_id, owner_id)
    config = portable_config(run)
    dataset = session.get(DatasetVersion, run.dataset_version_id)
    if (
        dataset is None
        or dataset.owner_id != owner_id
        or dataset.content_sha256 != config.dataset.sha256
    ):
        raise DomainError(
            "portable_result_invalid",
            "Portable dataset ownership or identity differs",
            409,
        )
    if row.status != "succeeded" or not row.sandbox_stopped or row.finished_at is None:
        raise DomainError(
            "portable_result_unavailable",
            "Only completed successful portable evaluations expose results",
            409,
        )
    try:
        ref = row.payload["portable_result"]
        artifact = session.get(Artifact, UUID(ref["artifact_id"]))
        if (
            artifact is None
            or artifact.run_id != row.run_id
            or artifact.attempt_id != row.attempt_id
            or artifact.name != f"evaluation-{row.id}-portable-result.json"
            or artifact.media_type != "application/json"
            or artifact.sha256 != ref["sha256"]
            or artifact.size_bytes != ref["size_bytes"]
            or not 0 < artifact.size_bytes <= 8 * 1_048_576
            or row.image_digest != run.research_image_digest
            or row.payload.get("status") != "succeeded"
            or row.payload.get("exit_code") != 0
            or row.payload.get("image_digest") != run.research_image_digest
            or row.payload.get("result_trust")
            != "isolated_policy_trusted_akquant_engine"
            or row.payload.get("ledger_origin") != "trusted_engine"
            or row.payload.get("runtime_certified") is not False
            or row.payload.get("policy_image_digest")
            != run.portable_policy_image_digest
        ):
            raise ValueError("Portable result artifact identity differs")
        content = read_verified(
            settings.storage_root,
            artifact.storage_key,
            artifact.sha256,
            settings.max_artifact_bytes,
        )
        bundle = verify_portable_bundle_binding(content, config, row)
        if row.payload.get("metrics") != bundle["summary"] or row.payload.get(
            "combined_score"
        ) != portable_score(bundle):
            raise ValueError("Portable summary does not match persisted engine bytes")
    except (KeyError, TypeError, ValueError, OSError, DomainError):
        raise DomainError(
            "portable_result_invalid",
            "Portable result evidence failed integrity verification",
            409,
        ) from None
    return OwnedPortableResult(
        row, artifact, bundle, content, run, dataset.is_synthetic
    )


def portable_result_view(result, section="account_series", offset=0, limit=100):
    allowed = {
        "account_series",
        "positions",
        "orders",
        "fills",
        "closed_trades",
        "native_metrics",
        "decision_trace",
        "logical_orders",
    }
    if (
        section not in allowed
        or type(offset) is not int
        or not 0 <= offset <= 100000
        or type(limit) is not int
        or not 1 <= limit <= 500
    ):
        raise DomainError(
            "invalid_result_query", "Portable result query exceeds bounds", 422
        )
    bundle = result.bundle
    return PortableResultQuery(
        run_id=result.run.id,
        image_digest=result.run.research_image_digest,
        policy_image_digest=result.run.portable_policy_image_digest,
        environment_digest=portable_config(result.run).environment_digest,
        is_synthetic=result.is_synthetic,
        currency=bundle["manifest"]["currency"],
        evaluation_id=result.evaluation.id,
        schema_version=bundle["schema_version"],
        result_trust=bundle["result_trust"],
        ledger_origin=bundle["ledger_origin"],
        artifact_sha256=result.artifact.sha256,
        identity=bundle["identity"],
        window=bundle["window"],
        summary=bundle["summary"],
        combined_score=portable_score(bundle),
        section=section,
        rows=bundle[section][offset : offset + limit],
        count=len(bundle[section]),
        offset=offset,
        limit=limit,
    )


def portable_usage(session, run_id, owner_id):
    owned_portable_run(session, run_id, owner_id)
    rows = session.execute(
        select(ModelCall.usage, ModelCall.adopted)
        .where(ModelCall.run_id == run_id)
        .limit(10001)
    ).all()
    if len(rows) > 10000:
        raise DomainError(
            "usage_too_large", "Persisted usage exceeds its bounded contract", 409
        )
    totals = {key: 0 for key in ("input_tokens", "output_tokens", "charged_tokens")}
    known = adopted = 0
    sources = set()
    for usage, is_adopted in rows:
        if type(usage) is not dict:
            raise DomainError("usage_invalid", "Invalid persisted usage", 409)
        present = True
        for key in totals:
            value = usage.get(key)
            if value is None and key != "charged_tokens":
                present = False
            elif type(value) is not int or not 0 <= value <= 10**9:
                raise DomainError("usage_invalid", "Invalid persisted token count", 409)
            else:
                totals[key] += value
        known += int(present)
        adopted += int(is_adopted)
        if isinstance(usage.get("usage_source"), str):
            sources.add(usage["usage_source"][:160])
    return ResearchUsagePublic(
        run_id=run_id,
        calls=len(rows),
        adopted_calls=adopted,
        input_tokens=totals["input_tokens"] if known == len(rows) else None,
        output_tokens=totals["output_tokens"] if known == len(rows) else None,
        charged_tokens=totals["charged_tokens"],
        known_usage_calls=known,
        unknown_usage_calls=len(rows) - known,
        usage_sources=sorted(sources),
    )
