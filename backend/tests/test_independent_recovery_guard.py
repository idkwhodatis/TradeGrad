"""Actual kernel guard with labelled persistence doubles, not a PG substitute."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.app import services
from backend.app.comparison_guard import comparison_attempt_guard
from backend.tests.test_model_containment_receipts import make_attempt


@pytest.mark.parametrize(
    "schema",
    [
        "tradegrad.independent-evaluation.v1",
        "tradegrad.portable.independent-evaluation.v1",
    ],
)
@pytest.mark.parametrize(
    "host_live,docker_stopped,expected",
    [(True, True, False), (False, False, False), (False, True, True)],
)
def test_host_query_and_sandbox_need_two_independent_stop_proofs(
    tmp_path, monkeypatch, schema, host_live, docker_stopped, expected
):
    run, attempt = make_attempt()
    run.config = {"schema_version": schema}
    before = attempt.lease_token
    session = MagicMock()
    session.scalar.side_effect = [run.id, run, attempt]
    session.scalars.return_value.all.return_value = []
    events = []
    monkeypatch.setattr(
        services, "append_event", lambda *_args: events.append(_args[2])
    )
    stops = []

    def stop(identifier):
        stops.append(identifier)
        return docker_stopped

    settings = SimpleNamespace(storage_root=tmp_path)
    if host_live:
        with comparison_attempt_guard(tmp_path, run.id, attempt.id):
            assert (
                services.reconcile_attempt(session, attempt.id, stop, settings=settings)
                is expected
            )
        assert stops == []  # Docker cannot substitute for live host-query ownership
    else:
        assert (
            services.reconcile_attempt(session, attempt.id, stop, settings=settings)
            is expected
        )
        assert stops == [str(attempt.id)]
    assert attempt.sandbox_stopped is expected
    assert (attempt.lease_token != before) is expected
    assert (attempt.finished_at is not None) is expected
    assert events == ["run_finished" if expected else "recovery_blocked"]


def test_missing_storage_identity_never_releases_independent_quota(monkeypatch):
    run, attempt = make_attempt()
    run.config = {"schema_version": "tradegrad.independent-evaluation.v1"}
    session = MagicMock()
    session.scalar.side_effect = [run.id, run, attempt]
    monkeypatch.setattr(services, "append_event", lambda *_: None)

    def stop(_):
        pytest.fail("Missing host identity cannot fall back to Docker")

    assert not services.reconcile_attempt(session, attempt.id, stop)
    assert attempt.finished_at is None and run.status == "running"
