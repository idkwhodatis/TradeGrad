"""Allowlisted crash-recovery diagnostics, never raw service or database output."""

import math

_STATUSES = {
    "queued",
    "preparing",
    "running",
    "cancel_requested",
    "cancelled",
    "interrupted",
    "succeeded",
    "failed",
}


def _seconds(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(result):
        return None
    return round(max(-3600, min(3600, result)), 2)


def recovery_diagnostic(row, *, container_state="unknown"):
    """Input is one explicit read-only projection, not an ORM/HTTP payload."""
    if row is None:
        return {"database": "unavailable"}
    (
        run_status,
        attempt_status,
        lease_delta,
        heartbeat_age,
        stopped,
        finished,
        blocked,
    ) = row
    return {
        "database": "observed",
        "run_status": run_status if run_status in _STATUSES else "unknown",
        "attempt_status": attempt_status if attempt_status in _STATUSES else "unknown",
        "lease_remaining_seconds": _seconds(lease_delta),
        "worker_heartbeat_age_seconds": _seconds(heartbeat_age),
        "sandbox_stopped": stopped if type(stopped) is bool else None,
        "finished": finished if type(finished) is bool else None,
        "recovery_blocked": blocked if type(blocked) is bool else None,
        "container": container_state
        if container_state in {"absent", "running", "exited"}
        else "unknown",
    }
