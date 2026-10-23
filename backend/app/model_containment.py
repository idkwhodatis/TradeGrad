"""Durable local model-process receipts, independent of provider billing outcome.

The Run lock serializes launch admission with lease revocation/reconciliation.
An admission without its confirmed-stop receipt blocks recovery indefinitely;
time passing or observing a dead worker is not substituted for process evidence.
"""

from sqlalchemy import select

from backend.app.models import ExecutionAttempt, ResearchRun, RunEvent
from backend.app.services import DomainError, FenceLost, append_event, fenced_attempt

ADMITTED = "model_process_admitted"
STOPPED = "model_process_stopped"


def _receipts(session, attempt_id):
    return session.scalars(
        select(RunEvent)
        .where(
            RunEvent.attempt_id == attempt_id,
            RunEvent.type.in_((ADMITTED, STOPPED)),
        )
        .order_by(RunEvent.seq)
        .limit(20001)
    ).all()


def model_processes_confirmed_stopped(session, attempt_id):
    rows = _receipts(session, attempt_id)
    if len(rows) > 20000:
        return False
    active = set()
    for row in rows:
        if not isinstance(row.payload, dict) or set(row.payload) != {"call_id"}:
            return False
        call_id = row.payload["call_id"]
        if not isinstance(call_id, str) or not 0 < len(call_id) <= 160:
            return False
        if row.type == ADMITTED:
            if call_id in active:
                return False
            active.add(call_id)
        elif call_id in active:
            active.remove(call_id)
        else:
            return False
    return not active


def admit_model_process(session, attempt_id, generation, token, call_id):
    run, attempt = fenced_attempt(session, attempt_id, generation, token)
    if run.status in {"cancel_requested", "pause_requested"}:
        raise DomainError(
            "model_control_pending", "Control request prevents another model process"
        )
    if not isinstance(call_id, str) or not 0 < len(call_id) <= 160:
        raise DomainError("invalid_model_call", "Model call identity exceeds bounds")
    rows = _receipts(session, attempt.id)
    if len(rows) >= 20000 or any(row.payload.get("call_id") == call_id for row in rows):
        raise DomainError(
            "model_process_already_admitted", "Model process cannot be admitted twice"
        )
    append_event(session, run, ADMITTED, {"call_id": call_id}, attempt.id)


def confirm_model_process_stopped(session, attempt_id, generation, token, call_id):
    # This callback may arrive after lease expiry. It reports only a previously
    # admitted process's exit, never restores execution, heartbeat or lease rights.
    run_id = session.scalar(
        select(ExecutionAttempt.run_id).where(ExecutionAttempt.id == attempt_id)
    )
    run = session.scalar(
        select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
    )
    attempt = session.scalar(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.id == attempt_id)
        .with_for_update()
    )
    if (
        run is None
        or attempt is None
        or attempt.generation != generation
        or attempt.lease_token != token
        or run.active_attempt_id != attempt.id
        or attempt.finished_at is not None
    ):
        raise FenceLost()
    rows = [
        row
        for row in _receipts(session, attempt_id)
        if row.payload.get("call_id") == call_id
    ]
    if len(rows) == 2 and rows[0].type == ADMITTED and rows[1].type == STOPPED:
        return
    if len(rows) != 1 or rows[0].type != ADMITTED:
        raise DomainError(
            "model_process_receipt_invalid", "Model process stop lacks exact admission"
        )
    append_event(session, run, STOPPED, {"call_id": call_id}, attempt.id)
