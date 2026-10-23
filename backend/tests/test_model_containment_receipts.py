"""Pure persistence doubles test receipt ordering/fences, never PostgreSQL proof."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from backend.app import model_containment as boundary
from backend.app import services
from backend.app.models import utcnow


def receipt(kind, call_id="synthetic:0000"):
    return SimpleNamespace(type=kind, payload={"call_id": call_id})


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([], True),
        ([receipt(boundary.ADMITTED)], False),
        ([receipt(boundary.ADMITTED), receipt(boundary.STOPPED)], True),
        ([receipt(boundary.STOPPED)], False),
        ([receipt(boundary.ADMITTED), receipt(boundary.ADMITTED)], False),
        ([receipt(boundary.ADMITTED), receipt(boundary.STOPPED, "other")], False),
        ([SimpleNamespace(type=boundary.ADMITTED, payload={})], False),
        ([receipt(boundary.ADMITTED)] * 20001, False),
    ],
)
def test_only_exact_confirmed_receipts_prove_process_stop(monkeypatch, rows, expected):
    monkeypatch.setattr(boundary, "_receipts", lambda *_: rows)
    assert boundary.model_processes_confirmed_stopped(None, uuid4()) is expected


def make_attempt():
    now = utcnow()
    attempt = SimpleNamespace(
        id=uuid4(),
        run_id=uuid4(),
        generation=1,
        lease_token=uuid4(),
        lease_expires_at=now - timedelta(seconds=60),
        finished_at=None,
        sandbox_stopped=False,
        status="running",
    )
    run = SimpleNamespace(
        id=attempt.run_id,
        active_attempt_id=attempt.id,
        generation=1,
        status="running",
        config={"model": {"provider": "openai-compatible"}},
        error=None,
    )
    return run, attempt


@pytest.mark.parametrize("known_stopped", [False, True])
def test_actual_reconciler_does_not_release_unconfirmed_model_process(
    monkeypatch, known_stopped
):
    run, attempt = make_attempt()
    session = MagicMock()
    session.scalar.side_effect = [run.id, run, attempt]
    session.scalars.return_value.all.return_value = []
    events = []
    monkeypatch.setattr(
        services, "append_event", lambda *_args: events.append(_args[2])
    )
    rows = [receipt(boundary.ADMITTED)] + (
        [receipt(boundary.STOPPED)] if known_stopped else []
    )
    monkeypatch.setattr(boundary, "_receipts", lambda *_: rows)
    assert (
        services.reconcile_attempt(session, attempt.id, lambda _id: True)
        is known_stopped
    )
    assert attempt.sandbox_stopped is known_stopped
    assert run.status == ("interrupted" if known_stopped else "running")
    assert events == (["run_finished"] if known_stopped else ["recovery_blocked"])
    if not known_stopped:
        assert "model process termination receipt" in run.error
        assert attempt.finished_at is None


def test_admission_is_fenced_and_duplicate_call_cannot_be_launched(monkeypatch):
    run, attempt = make_attempt()
    order, rows = [], []

    def fence(*_):
        order.append("fenced")
        return run, attempt

    monkeypatch.setattr(boundary, "fenced_attempt", fence)
    monkeypatch.setattr(boundary, "_receipts", lambda *_: rows)
    monkeypatch.setattr(boundary, "append_event", lambda *_args: order.append(_args[2]))
    boundary.admit_model_process(None, attempt.id, 1, attempt.lease_token, "synthetic")
    assert order == ["fenced", boundary.ADMITTED]
    rows.append(receipt(boundary.ADMITTED, "synthetic"))
    with pytest.raises(services.DomainError, match="cannot be admitted twice"):
        boundary.admit_model_process(
            None, attempt.id, 1, attempt.lease_token, "synthetic"
        )
    run.status = "cancel_requested"
    with pytest.raises(services.DomainError, match="prevents another"):
        boundary.admit_model_process(None, attempt.id, 1, attempt.lease_token, "new")


def test_late_stop_receipt_does_not_renew_expired_lease_or_change_authority(
    monkeypatch,
):
    run, attempt = make_attempt()
    original = dict(attempt.__dict__)
    session = MagicMock()
    session.scalar.side_effect = [run.id, run, attempt]
    monkeypatch.setattr(boundary, "_receipts", lambda *_: [receipt(boundary.ADMITTED)])
    emitted = []
    monkeypatch.setattr(
        boundary, "append_event", lambda *_args: emitted.append((_args[2], _args[3]))
    )
    boundary.confirm_model_process_stopped(
        session, attempt.id, 1, attempt.lease_token, "synthetic:0000"
    )
    assert emitted == [(boundary.STOPPED, {"call_id": "synthetic:0000"})]
    if attempt.__dict__ != original:
        pytest.fail("Stop receipt changed attempt authority", pytrace=False)
    assert run.status == "running"


def test_unmatched_or_fenced_stop_receipt_cannot_be_forged(monkeypatch):
    run, attempt = make_attempt()
    session = MagicMock()
    monkeypatch.setattr(boundary, "_receipts", lambda *_: [])
    session.scalar.side_effect = [run.id, run, attempt]
    with pytest.raises(services.DomainError, match="lacks exact admission"):
        boundary.confirm_model_process_stopped(
            session, attempt.id, 1, attempt.lease_token, "unknown"
        )
    session.scalar.side_effect = [run.id, run, attempt]
    with pytest.raises(services.FenceLost):
        boundary.confirm_model_process_stopped(
            session, attempt.id, 1, uuid4(), "unknown"
        )
