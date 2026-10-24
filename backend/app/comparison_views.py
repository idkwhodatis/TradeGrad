"""Safe status projections and separately admitted comparison evidence reads."""

from backend.app.comparison_schemas import ComparisonPublic
from backend.app.schemas import EventPublic

SAFE_ERRORS = {
    "comparison_cancelled",
    "comparison_timeout",
    "comparison_capacity",
    "comparison_integrity",
    "comparison_source_unavailable",
    "comparison_failed",
    "comparison_recovery_blocked",
    "comparison_interrupted",
}
SAFE_STATUSES = {
    "queued",
    "preparing",
    "running",
    "finalizing",
    "cancel_requested",
    "cancelled",
    "failed",
    "interrupted",
    "succeeded",
}
SAFE_EVENTS = {
    "stage_changed",
    "run_finished",
    "comparison_admitted",
    "comparison_retry_admitted",
    "comparison_artifact_ready",
    "recovery_blocked",
}


def safe_error(value):
    if value is None:
        return None
    return (
        value
        if isinstance(value, str) and value in SAFE_ERRORS
        else "comparison_failed"
    )


def public_comparison_event(event):
    """No source outcomes in generic SSE, even for malformed persisted events."""
    import re
    from uuid import UUID

    known = isinstance(event.type, str) and event.type in SAFE_EVENTS
    payload = event.payload if known and isinstance(event.payload, dict) else {}
    safe = {}
    if isinstance(payload.get("status"), str) and payload["status"] in SAFE_STATUSES:
        safe["status"] = payload["status"]
    if type(payload.get("generation")) is int and 0 < payload["generation"] < 2**31:
        safe["generation"] = payload["generation"]
    for name in ("comparison_id", "manifest_artifact_id"):
        value = payload.get(name)
        try:
            if isinstance(value, str):
                safe[name] = str(UUID(value))
        except ValueError:
            pass
    value = payload.get("manifest_sha256")
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        safe["manifest_sha256"] = value
    if (
        isinstance(payload.get("error_code"), str)
        and payload["error_code"] in SAFE_ERRORS
    ):
        safe["error_code"] = payload["error_code"]
    return EventPublic(
        schema_version=1,
        run_id=event.run_id,
        attempt_id=event.attempt_id,
        seq=event.seq,
        occurred_at=event.occurred_at,
        type=event.type if known else "comparison_status",
        payload=safe,
    )


def comparison_public(comparison, run):
    from backend.app.comparison_services import comparison_config

    config = comparison_config(run, comparison)
    ready = run.status == "succeeded" and comparison.manifest_artifact_id is not None
    return ComparisonPublic(
        id=comparison.id,
        run_id=run.id,
        status=run.status,
        generation=run.generation,
        input_sha256=comparison.input_sha256,
        engine_input_sha256=comparison.engine_input_sha256,
        request=config.request,
        active_attempt_id=run.active_attempt_id,
        next_event_seq=run.next_event_seq,
        artifact_ready=ready,
        manifest_artifact_id=comparison.manifest_artifact_id if ready else None,
        manifest_sha256=comparison.manifest_sha256 if ready else None,
        error_code=safe_error(run.error),
        created_at=comparison.created_at,
        updated_at=run.updated_at,
    )
