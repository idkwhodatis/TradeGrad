"""Pure independent contracts/staging; not PostgreSQL or Docker proof."""

import copy
import hashlib
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.independent_schemas import (
    ControlledExperimentCreate,
    DiagnosticReplayCreate,
)
from backend.app.independent_services import protocol_for, require_independent_admission
from backend.app.services import DomainError
from packages.research_trace.contracts import CaptureConfig, digest
from packages.tradegrad_adapter.config import (
    DateWindow,
    RunConfig,
    VersionRef,
    canonical_bytes,
)
from packages.tradegrad_adapter.independent_config import (
    IndependentEvaluationConfig,
    IndependentSourcePin,
    parse_independent_config,
)
from packages.tradegrad_adapter.native_contract import NativeStrategyContract
from tests.tradegrad.test_config_windows import _bundle, _sessions
from workers.research.independent_staging import stage_independent_input


def source_pin(code="pass\n"):
    return IndependentSourcePin(
        evaluation_id=uuid4(),
        execution_attempt_id=uuid4(),
        result_manifest_sha256="a" * 64,
        trace_manifest_sha256=None,
        source_kind="strategy_version",
        source_id=uuid4(),
        code_sha256=hashlib.sha256(code.encode()).hexdigest(),
    )


def config_and_files(**changes):
    source, manifest, files = _bundle()
    files["universe/fixed_list_2024-01.csv"] = (
        b"updateDate,code,code_name\n2024-01-01,A,A\n2024-01-01,B,B\n"
    )
    manifest["files"]["universe/fixed_list_2024-01.csv"] = hashlib.sha256(
        files["universe/fixed_list_2024-01.csv"]
    ).hexdigest()
    raw_source = source.model_dump(mode="json")
    raw_source["dataset"]["sha256"] = hashlib.sha256(
        canonical_bytes(manifest)
    ).hexdigest()
    source = RunConfig.model_validate(raw_source)
    code = "pass\n"
    pin = source_pin(code)
    values = {
        "independent_id": uuid4(),
        "purpose": "diagnostic",
        "source": pin,
        "source_run_id": uuid4(),
        "original_config_sha256": source.sha256,
        "source_config": source,
        "source_config_sha256": source.sha256,
        "selected_strategy": VersionRef(id=str(uuid4()), sha256=pin.code_sha256),
        "split": "train",
        "reconstruction_window": source.windows.train,
        "research_capture": CaptureConfig(mode="research"),
        "environment_digest": source.environment_digest,
        "research_image_digest": "sha256:" + "a" * 64,
        "protocol_sha256": "b" * 64,
    }
    values.update(changes)
    return IndependentEvaluationConfig(**values), manifest, files, code


def test_independent_schema_not_optimizer_preserves_original_bytes():
    config, _, _, _ = config_and_files()
    assert config.execution_mode == "independent_evaluation"
    assert (
        config.source_config.canonical_bytes()
        == RunConfig.model_validate(
            config.model_dump(mode="json")["source_config"]
        ).canonical_bytes()
    )
    assert parse_independent_config(json.loads(config.canonical_bytes())) == config
    with pytest.raises(ValidationError):
        RunConfig.model_validate(config.model_dump(mode="json"))
    with pytest.raises(ValidationError):
        config.source_config.warmup.sessions = 0


@pytest.mark.parametrize(
    "edit",
    [
        {"source_config_sha256": "c" * 64},
        {"environment_digest": "c" * 64},
        {"state_policy": "optimizer_checkpoint_resume"},
        {"max_runtime_seconds": 0},
        {
            "purpose": "controlled",
            "split": "holdout",
            "reconstruction_window": DateWindow(start="2024-01-10", end="2024-01-12"),
        },
        {"selected_strategy": VersionRef(id=str(uuid4()), sha256="f" * 64)},
        {"rng_restore": {"python": "unverified"}},
    ],
)
def test_rejects_nonfrozen_authority(edit):
    with pytest.raises(ValidationError):
        config_and_files(**edit)


def test_pins_require_explicit_absence_and_paired_spec():
    raw = source_pin().model_dump(mode="json")
    del raw["trace_manifest_sha256"]
    with pytest.raises(ValidationError):
        IndependentSourcePin.model_validate(raw)
    with pytest.raises(ValidationError):
        IndependentSourcePin.model_validate(
            source_pin().model_dump(mode="json") | {"spec_revision": 1}
        )


@pytest.mark.parametrize("count", [0, 1, 4])
def test_controls_have_exactly_two_or_three_arms(count):
    with pytest.raises(ValidationError):
        ControlledExperimentCreate(
            source=source_pin(),
            name="Manual control",
            hypothesis="one factor",
            planned_change="threshold",
            arms=[{"name": str(i), "code": "pass"} for i in range(count)],
        )


def test_diagnostic_never_accepts_model_or_optimizer_options():
    for field in ("optimizer", "model", "provider", "resume_checkpoint_id"):
        with pytest.raises(ValidationError):
            DiagnosticReplayCreate.model_validate(
                {"source": source_pin().model_dump(mode="json"), field: {}}
            )


def test_observation_filter_keeps_original_start_warmup_complete_universe(tmp_path):
    config, manifest, files, code = config_and_files(
        research_capture=CaptureConfig(
            mode="research",
            observation_window={"start": "2024-01-06", "end": "2024-01-07"},
            instrument_ids=["A"],
        )
    )
    for name, content in files.items():
        path = tmp_path / "dataset" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    staged = stage_independent_input(
        config=config,
        manifest=manifest,
        storage_root=tmp_path,
        dataset_storage_key="dataset",
        code=code,
        target=tmp_path / "attempt" / "input",
        max_bytes=10**7,
    )
    request = json.loads((staged / "request.json").read_bytes())
    assert request["start_date"] == "2024-01-05"
    assert request["end_date"] == "2024-01-08"
    assert request["evaluation_config"]["result_policy"] == "independent_observation.v1"
    assert request["evaluation_config"]["research_capture"]["observation_window"] == {
        "start": "2024-01-06",
        "end": "2024-01-07",
    }
    assert _sessions((staged / "data/A.csv").read_bytes()) == [
        f"2024-01-{day:02}" for day in range(2, 8)
    ]
    assert (staged / "data/B.csv").is_file()
    assert not (staged / "checkpoint.json").exists()
    assert (staged / "strategy.py").read_text() == code
    assert staged.stat().st_mode & 0o222 == 0


def _contract(value=2, warmup=3):
    return NativeStrategyContract(
        lookback_bars=3,
        warmup_bars=warmup,
        requires_shorting=False,
        parameters=[
            {
                "name": "threshold",
                "kind": "integer",
                "value": value,
                "minimum": 1.0,
                "maximum": 10.0,
            }
        ],
    )


def test_protocol_excludes_parameter_values_not_declaration_contracts():
    config, _, _, _ = config_and_files()

    def protocol(value=2, warmup=3):
        raw = config.source_config.model_dump(mode="json")
        raw["strategy_contract"] = _contract(value, warmup).model_dump(mode="json")
        return protocol_for(
            RunConfig.model_validate(raw), None, {"fees": {"commission": 0.01}}, None
        )

    assert digest(protocol(2)) == digest(protocol(3))
    assert digest(protocol(2)) != digest(protocol(2, 2))
    a = protocol()
    b = copy.deepcopy(a)
    b["execution_conditions"]["fees"]["commission"] = 0.02
    assert digest(a) != digest(b)
    assert (
        a["fixed_source_config"]["strategy_contract"]["parameters"][0]["minimum"] == 1.0
    )


def admission_fixture(monkeypatch):
    config, _, _, _ = config_and_files()
    raw = config.source_config.model_dump(mode="json")
    dataset_id, owner_id = uuid4(), uuid4()
    raw["dataset"]["id"] = str(dataset_id)
    source = RunConfig.model_validate(raw)
    config = IndependentEvaluationConfig.model_validate(
        config.model_dump(mode="json")
        | {
            "source_config": source.model_dump(mode="json"),
            "source_config_sha256": source.sha256,
        }
    )
    run = SimpleNamespace(
        id=uuid4(),
        owner_id=owner_id,
        generation=1,
        config=config.model_dump(mode="json"),
        request_sha256=config.sha256,
        dataset_version_id=dataset_id,
        experiment_id=uuid4(),
        split_version_id=uuid4(),
    )
    dataset = SimpleNamespace(
        id=dataset_id,
        owner_id=owner_id,
        start_date=date(2024, 1, 1),
        content_sha256=source.dataset.sha256,
    )
    exposure = {
        "schema_version": "tradegrad.exposure.v1",
        "classification": "no_tracked_overlap",
        "matching_exposure_ids": [],
        "matching_purposes": [],
        "matching_count": 0,
        "identities": [],
        "as_of": datetime.now(UTC).isoformat(),
    }
    item = SimpleNamespace(
        owner_id=owner_id,
        source_key=f"independent:{run.id}:1",
        run_id=run.id,
        experiment_id=run.experiment_id,
        split_version_id=run.split_version_id,
        dataset_version_id=dataset_id,
        start_date=dataset.start_date,
        end_date=config.reconstruction_window.end,
        identities=[{"a": "b"}],
        purpose="independent_evaluation_admission",
        provenance={
            "generation": 1,
            "config_sha256": config.sha256,
            "source_evaluation_id": str(config.source.evaluation_id),
            "source_result_sha256": config.source.result_manifest_sha256,
            "protocol_sha256": config.protocol_sha256,
            "requested_window": config.window,
            "coverage": "conservative_dataset_prefix",
            "admission_before_dispatch": True,
            "prior_exposure_snapshot": exposure,
        },
    )
    monkeypatch.setattr(
        "backend.app.experiment_services._identities", lambda *args: [{"a": "b"}]
    )
    session = SimpleNamespace(
        scalar=lambda query: item, get=lambda model, identifier: dataset
    )
    return session, run, item


def test_exact_admission_accepts_complete_writer_contract(monkeypatch):
    session, run, item = admission_fixture(monkeypatch)
    assert require_independent_admission(session, run) is item


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", uuid4()),
        ("run_id", uuid4()),
        ("dataset_version_id", uuid4()),
        ("experiment_id", None),
        ("split_version_id", None),
        ("source_key", "wrong"),
        ("start_date", date(1900, 1, 1)),
        ("end_date", date(1900, 1, 2)),
        ("identities", []),
        ("purpose", "unrelated"),
    ],
)
def test_admission_rejects_every_wrong_resource_field(monkeypatch, field, value):
    session, run, item = admission_fixture(monkeypatch)
    setattr(item, field, value)
    with pytest.raises(DomainError, match="Exact-generation"):
        require_independent_admission(session, run)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", True),
        ("generation", 2),
        ("source_result_sha256", "f" * 64),
        ("source_evaluation_id", str(uuid4())),
        ("protocol_sha256", "f" * 64),
        ("requested_window", {}),
        ("coverage", "window_only"),
        ("admission_before_dispatch", False),
        ("prior_exposure_snapshot", {}),
    ],
)
def test_admission_rejects_every_wrong_provenance_field(monkeypatch, field, value):
    session, run, item = admission_fixture(monkeypatch)
    item.provenance[field] = value
    with pytest.raises(DomainError, match="Exact-generation"):
        require_independent_admission(session, run)


@pytest.mark.parametrize("ancestors,allowed", [(0, True), (15, True), (16, False)])
def test_prospective_source_chain_budget(monkeypatch, ancestors, allowed):
    from backend.app import independent_services as service

    calls = []
    runs = [
        SimpleNamespace(config={"schema_version": service.NATIVE_SCHEMA})
        for _ in range(ancestors)
    ]
    runs.append(SimpleNamespace(config={"schema_version": "tradegrad.run.v1"}))
    iterator = iter(runs)

    def execute(query):
        calls.append(query)
        return SimpleNamespace(one_or_none=lambda: (None, next(iterator)))

    monkeypatch.setattr(
        service,
        "independent_config",
        lambda run: SimpleNamespace(source=SimpleNamespace(evaluation_id=uuid4())),
    )
    session = SimpleNamespace(execute=execute)
    if allowed:
        service.validate_new_source_depth(session, uuid4(), uuid4())
        assert len(calls) == ancestors + 1
    else:
        with pytest.raises(DomainError) as error:
            service.validate_new_source_depth(session, uuid4(), uuid4())
        assert error.value.code == "independent_source_depth"
        assert len(calls) == 16


def test_reader_source_cache_resets_after_failed_read():
    from backend.app.independent_services import _source_cache
    from backend.app.independent_views import _depth, load_owned_independent_result

    observed = []

    def execute(query):
        observed.append((_depth.get(), _source_cache.get()))
        return SimpleNamespace(one_or_none=lambda: None)

    session = SimpleNamespace(execute=execute)
    assert _source_cache.get() is None
    for _ in range(2):
        with pytest.raises(DomainError):
            load_owned_independent_result(session, uuid4(), uuid4(), None)
        assert _depth.get() == 0
        assert _source_cache.get() is None
    assert observed[0][0] == observed[1][0] == 1
    assert observed[0][1] is not observed[1][1]


def test_source_cache_binds_session_owner_and_complete_pin(monkeypatch):
    from backend.app import independent_services as service

    pin, session, owner = source_pin(), object(), uuid4()
    sentinel = object()
    cache = {(id(session), str(owner), digest(pin)): sentinel}
    token = service._source_cache.set(cache)
    calls = []

    def miss(*args):
        calls.append(args)
        raise LookupError("strict loader must re-admit this different source")

    monkeypatch.setattr(service, "load_comparison_side", miss)
    try:
        assert service.resolve_source(session, owner, pin, None) is sentinel
        for different_session, different_owner, different_pin in [
            (object(), owner, pin),
            (session, uuid4(), pin),
            (
                session,
                owner,
                pin.model_copy(update={"result_manifest_sha256": "b" * 64}),
            ),
        ]:
            with pytest.raises(LookupError):
                service.resolve_source(
                    different_session, different_owner, different_pin, None
                )
        assert len(calls) == 3
    finally:
        service._source_cache.reset(token)


def test_independent_native_query_exact_series_paging_and_metric_projection(
    monkeypatch,
):
    from backend.app import independent_routes as routes
    from packages.tradegrad_adapter.independent_config import NATIVE_SCHEMA

    evaluation_id, attempt_id, owner = uuid4(), uuid4(), uuid4()
    rows = [[index, index / 3] for index in range(602)]
    result = SimpleNamespace(
        evaluation=SimpleNamespace(id=evaluation_id, attempt_id=attempt_id),
        artifact=SimpleNamespace(sha256="a" * 64),
        run=SimpleNamespace(
            id=uuid4(),
            config={"schema_version": NATIVE_SCHEMA, "purpose": "diagnostic"},
        ),
        bundle={
            "metrics": [
                {"name": "total_return", "value": 0.123456789},
                {"name": "win_rate", "value": None},
            ],
            "result_trust": "native_engine",
            "equity": {
                "columns": [{"name": "timestamp"}, {"name": "equity"}],
                "rows": rows,
            },
        },
    )
    monkeypatch.setattr(routes, "load_owned_independent_result", lambda *args: result)
    accesses = []
    monkeypatch.setattr(
        routes, "record_verified_result_access", lambda *args: accesses.append(True)
    )
    session = SimpleNamespace(commit=lambda: None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=None)))
    for offset in (0, 25, 600, 602):
        value = routes.query_result(
            evaluation_id,
            SimpleNamespace(id=owner),
            session,
            request,
            attempt_id,
            "a" * 64,
            "equity",
            offset,
            25,
        )
        assert value.rows == rows[offset : offset + 25]
        assert value.total == 602
        assert value.columns == ["timestamp", "equity"]
        assert value.metrics == {"total_return": 0.123456789, "win_rate": None}
        assert value.reward_status == "not_applicable"
    assert len(accesses) == 4
    with pytest.raises(DomainError) as error:
        routes.query_result(
            evaluation_id,
            SimpleNamespace(id=owner),
            session,
            request,
            uuid4(),
            "a" * 64,
            "equity",
            0,
            25,
        )
    assert error.value.code == "result_pin_mismatch"
    assert len(accesses) == 4
