"""Direct real route/DTO contracts with inert persistence; not PostgreSQL proof."""

import inspect
import secrets
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app import main, portable_routes, research_routes, result_access
from backend.app.core.config import Settings
from backend.app.portable_schemas import PortableRunPublic
from backend.app.research_schemas import ResearchRunPublic
from backend.app.schemas import RunPublic
from backend.app.services import DomainError
from packages.tradegrad_adapter.portable_config import PortableRunConfig
from tests.portable.test_config import config_values

CONTROLS = [
    ("generic", "create_run", "submit_run"),
    ("generic", "cancel_run", "request_cancel"),
    ("generic", "resume_run", "resume_run"),
    ("research", "create_research_run", "submit_research_run"),
    ("research", "pause_research_run", "request_pause"),
    ("research", "cancel_research_run", "request_cancel"),
    ("research", "resume_research", "resume_research_run"),
    ("portable", "create_run", "submit_portable_run"),
    ("portable", "pause", "pause_portable_run"),
    ("portable", "cancel", "request_cancel"),
    ("portable", "resume", "resume_portable_run"),
]


def fixture_run(mode):
    now = datetime.now(UTC)
    result = {"metrics": {"sharpe_ratio": 1.12}, "is_synthetic": True}
    config = {}
    if mode == "portable":
        config = PortableRunConfig.model_validate(config_values()).model_dump(
            mode="json"
        )
        result |= {
            "execution_mode": "seed_evaluation",
            "stop_reason": "fixture_complete",
            "objective_definition": "portable_return_drawdown.v1",
        }
    return SimpleNamespace(
        id=uuid4(),
        owner_id=uuid4(),
        status="succeeded",
        generation=2,
        config=config,
        dataset_version_id=uuid4(),
        strategy_version_id=uuid4(),
        active_attempt_id=uuid4(),
        next_event_seq=10,
        result=result,
        error=None,
        created_at=now,
        updated_at=now,
        execution_mode="seed_evaluation",
    )


def control_fixture(monkeypatch, mode, name, service, *, actual_cancel=False):
    run = fixture_run(mode)
    events = []
    settings = Settings(
        database_url="postgresql+psycopg://127.0.0.1/control-contract",
        secret_key=secrets.token_urlsafe(48),
    )
    module = {
        "generic": main,
        "research": research_routes,
        "portable": portable_routes,
    }[mode]
    if not actual_cancel:
        monkeypatch.setattr(module, service, lambda *args, **kwargs: run)
    for owned in ("owned_native_run", "owned_research_run", "owned_portable_run"):
        if hasattr(module, owned):
            monkeypatch.setattr(module, owned, lambda *args, **kwargs: run)
    if mode == "generic":
        app = main.create_app(settings=settings)
        endpoint = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "operation_id", None) == name
        )
    else:
        endpoint = getattr(module, name)
    session = SimpleNamespace(
        scalar=lambda *args, **kwargs: run,
        commit=lambda: events.append("commit"),
    )
    values = {
        "run_id": run.id,
        "user": SimpleNamespace(id=run.owner_id),
        "session": session,
        "request": SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(settings=settings))
        ),
        "body": None,
        "idempotency_key": uuid4().hex,
        "experiment_id": None,
        "split_version_id": None,
    }
    kwargs = {key: values[key] for key in inspect.signature(endpoint).parameters}

    def record(session, item, owner, **options):
        assert item is run and owner == run.owner_id
        assert options["source"] == "run-control-response"
        events.append("record")

    monkeypatch.setattr(result_access, "record_result_access", record)
    model = {
        "generic": RunPublic,
        "research": ResearchRunPublic,
        "portable": PortableRunPublic,
    }[mode]
    return endpoint, kwargs, run, events, model


@pytest.mark.parametrize("mode,name,service", CONTROLS)
def test_completed_or_idempotent_control_response_records_before_commit(
    monkeypatch, mode, name, service
):
    endpoint, kwargs, _run, events, model = control_fixture(
        monkeypatch, mode, name, service
    )
    response = model.model_validate(endpoint(**kwargs)).model_dump(mode="json")
    assert response["result"]["metrics"]["sharpe_ratio"] == 1.12
    assert events == ["record", "commit"]


@pytest.mark.parametrize(
    "mode,name,service", [row for row in CONTROLS if "cancel" in row[1]]
)
def test_actual_terminal_cancel_service_does_not_bypass_result_access(
    monkeypatch, mode, name, service
):
    endpoint, kwargs, run, events, model = control_fixture(
        monkeypatch, mode, name, service, actual_cancel=True
    )
    assert endpoint(**kwargs) is run
    assert model.model_validate(run).result.metrics == {"sharpe_ratio": 1.12}
    assert events == ["record", "commit"]


@pytest.mark.parametrize("mode,name,service", CONTROLS)
def test_control_metadata_only_response_does_not_invent_exposure(
    monkeypatch, mode, name, service
):
    endpoint, kwargs, run, events, _ = control_fixture(monkeypatch, mode, name, service)
    run.result = None
    assert endpoint(**kwargs) is run
    assert events == ["commit"]


@pytest.mark.parametrize("mode,name,service", CONTROLS)
def test_control_response_cannot_escape_failed_exposure_record_or_commit(
    monkeypatch, mode, name, service
):
    endpoint, kwargs, _, events, _ = control_fixture(monkeypatch, mode, name, service)

    def reject(*args, **kwargs):
        raise DomainError("result_integrity", "Fixture rejected", 409)

    monkeypatch.setattr(result_access, "record_result_access", reject)
    with pytest.raises(DomainError):
        endpoint(**kwargs)
    assert events == []

    monkeypatch.setattr(
        result_access,
        "record_result_access",
        lambda *args, **kwargs: events.append("record"),
    )
    kwargs["session"].commit = lambda: (_ for _ in ()).throw(
        RuntimeError("fixture commit failure")
    )
    with pytest.raises(RuntimeError, match="commit failure"):
        endpoint(**kwargs)
    assert events == ["record"]
