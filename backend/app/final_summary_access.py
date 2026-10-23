"""Safe lifecycle projections; metric reads still use the typed final loaders.

Lists are metadata-only and never open result artifacts. Detail and lifecycle
responses expose a result only after the same terminal, byte and admission gates
as an explicit result query. None of these projections changes persisted history.
"""

from uuid import UUID

from backend.app.final_evaluation_views import validate_final_terminal
from backend.app.models import Evaluation, ExecutionAttempt
from backend.app.portable_services import portable_score
from backend.app.services import DomainError


def public_projection(model, item, **overrides):
    """Validate the projected response, not a possibly stale persisted result."""
    values = {
        name: getattr(item, name)
        for name in model.model_fields
        if name not in overrides and hasattr(item, name)
    }
    return model.model_validate({**values, **overrides})


def accepted_result_access(session, run, owner_id, settings, access, evaluation=None):
    """Return verified current evidence, or leave the lifecycle view metadata-only.

    Terminal checks are cheap and happen before artifact reads. A broken result
    must not hide cancellation/attempt history; dedicated result routes continue
    to return the precise integrity/admission error instead of redacting it.
    """
    if run.owner_id != owner_id:
        raise DomainError("not_found", "Final evaluation not found", 404)
    if run.status != "succeeded" or not isinstance(run.result, dict):
        return None
    try:
        evaluation_id = UUID(run.result["evaluation_id"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    if evaluation is None:
        evaluation = session.get(Evaluation, evaluation_id)
    if evaluation is None or evaluation.id != evaluation_id:
        return None
    attempt = session.get(ExecutionAttempt, evaluation.attempt_id)
    if attempt is None:
        return None
    try:
        validate_final_terminal(evaluation, run, attempt)
        return access(session, evaluation.id, owner_id, settings, "result_preview")
    except DomainError:
        # No result data is returned when any publication gate fails. In
        # particular, a physical success from a cancelled/retried job is history.
        return None


def verified_summary(result, snapshot):
    """Derive values from verified bytes, never the denormalized run.result."""
    run, bundle = result.run, result.bundle
    summary = {
        "execution_mode": "final_evaluation",
        "evaluation_id": str(result.evaluation.id),
        "selection_id": str(run.final_selection_id),
        "is_synthetic": result.dataset.is_synthetic,
        "holdout_executed": True,
        "image_digest": run.research_image_digest,
        "exposure_snapshot": snapshot,
    }
    if run.config["schema_version"] == "tradegrad.portable.final-evaluation.v1":
        summary.update(
            metrics=bundle["summary"],
            combined_score=portable_score(bundle),
            evaluation_mode="portable",
            result_trust=bundle["result_trust"],
            ledger_origin=bundle["ledger_origin"],
            runtime_certified=False,
            external_framework_equivalence="not_certified",
            objective_definition=run.config["source_config"]["objective"][
                "definition_version"
            ],
            policy_image_digest=run.portable_policy_image_digest,
        )
    else:
        legacy = bundle["legacy_performance"]["value"]
        summary.update(
            metrics=legacy["metrics"],
            combined_score=legacy["combined_score"],
            evaluation_mode="native_akquant",
            result_trust=bundle["result_trust"],
        )
    return summary


def final_public_response(session, run, owner_id, settings, model, access):
    accepted = accepted_result_access(session, run, owner_id, settings, access)
    return public_projection(
        model, run, result=verified_summary(*accepted) if accepted else None
    )


def diagnostic_payload(evaluation):
    """Keep execution diagnostics and byte receipts, not arbitrary result data.

    The payload hash remains the original persisted hash. Explicit projection
    metadata distinguishes every response from the full immutable outcome.
    """
    payload = evaluation.payload
    if not isinstance(payload, dict):
        return None
    diagnostic = {
        "result_data_redacted": True,
        "payload_projection": "diagnostic_only",
    }
    for key in ("status", "exit_code", "image_digest", "policy_image_digest", "error"):
        value = payload.get(key)
        if key in payload and (value is None or isinstance(value, (str, int, bool))):
            diagnostic[key] = value
    for key in ("result_bundle", "portable_result"):
        reference = payload.get(key)
        if isinstance(reference, dict):
            diagnostic[key] = {
                name: value
                for name in ("artifact_id", "sha256", "size_bytes")
                if (value := reference.get(name)) is not None
                and isinstance(value, (str, int))
                and not isinstance(value, bool)
            }
    return diagnostic


def final_evidence_response(session, run, owner_id, settings, model, access, items):
    projected = []
    for item in items:
        payload = diagnostic_payload(item)
        accepted = accepted_result_access(
            session, run, owner_id, settings, access, evaluation=item
        )
        if accepted:
            result, snapshot = accepted
            summary = verified_summary(result, snapshot)
            payload = diagnostic_payload(result.evaluation) or {}
            payload.pop("result_data_redacted", None)
            payload["payload_projection"] = "verified_final_summary"
            if summary["evaluation_mode"] == "native_akquant":
                payload["metrics"] = result.bundle["legacy_performance"]["value"]
            else:
                payload["metrics"] = summary["metrics"]
            for key in (
                "combined_score",
                "result_trust",
                "ledger_origin",
                "runtime_certified",
                "external_framework_equivalence",
            ):
                if key in summary:
                    payload[key] = summary[key]
        projected.append(public_projection(model, item, payload=payload))
    return projected
