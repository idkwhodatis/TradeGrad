"""Opt-in original-seed engine subprocess -> persisted research projections.

    TRADEGRAD_LOCAL_ENGINE_BRIDGE=1 .venv-research/bin/python -m pytest \
        tests/tradegrad/test_result_views_native.py -q

Only SHA-pinned original US timing seed executes, in the existing locked .venv
engine child. Research parent lacks AKQuant and validates serialized evidence.
Coordinator identity is constructed for in-memory ORM records; no PostgreSQL,
Docker/isolation, HTTP, browser, generated candidate or remote provider evidence.
"""

import copy
import hashlib
import html
import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from backend.app.models import Artifact, DatasetVersion, Evaluation, ResearchRun
from backend.app.research_services import payload_hash
from backend.app.research_views import (
    MAX_REPORT_BYTES,
    VerifiedResult,
    render_result_report,
    result_view,
    validate_persisted_result,
)
from backend.app.services import DomainError
from backend.app.storage import read_verified
from packages.tradegrad_adapter.config import (
    DateWindow,
    MarketConfig,
    OptimizerConfig,
    RunConfig,
    VersionRef,
    WarmupConfig,
    WindowSet,
    canonical_bytes,
    evaluation_windows,
)
from packages.tradegrad_adapter.results import (
    MAX_WIRE_BYTES,
    artifact_inventory,
    bundle_sections,
    canonical_json_bytes,
    validate_result_bundle,
)
from runtimes.evaluator.runner import decode_result_protocol
from src.job import evaluator_environment
from tests.tradegrad import test_window_engine_bridge as engine_bridge

ROOT, FIXTURE = engine_bridge.ROOT, engine_bridge.FIXTURE
real_engine = engine_bridge.real_engine
pytestmark = pytest.mark.skipif(
    os.environ.get("TRADEGRAD_LOCAL_ENGINE_BRIDGE") != "1",
    reason="Local original-only bridge is opt-in; it cannot replace PG/Docker gates",
)


@pytest.fixture(scope="module")
def exported(real_engine, tmp_path_factory):
    output = tmp_path_factory.mktemp("original-native-result-view-subprocess")
    seed = ROOT / "seed_strategies/us_time.py"
    engine_bridge.original_seed("us_time")
    baseline = json.loads((FIXTURE / "manifest.json").read_bytes())
    assert baseline["source_commit"] == engine_bridge.SOURCE_COMMIT
    data = FIXTURE / "US/INDEX.csv"
    assert not data.is_symlink()
    assert _digest(data.read_bytes()) == baseline["files"]["US/INDEX.csv"]
    script = """
import hashlib, sys
from pathlib import Path
seed, data, output = (Path(value) for value in sys.argv[1:4])
assert not seed.is_symlink() and hashlib.sha256(seed.read_bytes()).hexdigest() == sys.argv[4]
assert not data.is_symlink() and hashlib.sha256(data.read_bytes()).hexdigest() == sys.argv[5]
from packages.tradegrad_adapter.results import export_native_result, wire_record
from src.evaluate_time import run_backtest
captured = {}
def capture(result, performance, kwargs):
    captured['bundle'] = export_native_result(
        result, performance,
        identity={'strategy_sha256': sys.argv[4], 'input_manifest_sha256': 'a' * 64},
        window={'start': '2024-02-02', 'end': '2024-04-27', 'interval': 'half_open',
                'warmup': {'history_policy': 'require', 'sessions': 20}},
        engine_kwargs=kwargs, currency='USD',
        reward_parameters={'ar_weight': 0.5, 'ar_reference': 20, 'mdd_reference': 15,
                           'freq_floor': 0.0187, 'freq_sharpness': 1.0},
    )
    captured['metrics'] = performance
run_backtest(str(seed), str(output), str(data), 'target_symbol',
             '2024-02-02', '2024-04-26', 'US', result_callback=capture)
print(wire_record(captured['metrics'], captured['bundle']))
"""
    stdout, stderr = output / "engine.stdout", output / "engine.stderr"
    with stdout.open("wb") as out, stderr.open("wb") as err:
        completed = subprocess.run(
            [
                str(engine_bridge.ENGINE_PYTHON),
                "-B",
                "-c",
                script,
                str(seed),
                str(data),
                str(output / "native"),
                engine_bridge.SEED_HASHES["us_time"],
                baseline["files"]["US/INDEX.csv"],
            ],
            cwd=ROOT,
            env=evaluator_environment({"AKQUANT_DATA_CACHE": str(output / "cache")}),
            stdout=out,
            stderr=err,
            timeout=60,
            check=False,
        )
    assert completed.returncode == 0, stderr.read_bytes()[-8192:].decode(
        errors="replace"
    )
    assert stdout.stat().st_size <= engine_bridge.MAX_STDOUT_BYTES
    decoded = output / "decoded"
    decoded.mkdir()
    metrics, bundle = decode_result_protocol(stdout.read_bytes(), decoded)
    assert bundle is not None
    assert (decoded / "result-bundle.json").read_bytes() == canonical_json_bytes(bundle)
    return bundle, canonical_json_bytes(metrics)


UNSAFE_LABEL = '<script>alert("not executed")</script><img src=x onerror=alert(1)>'


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _refresh_sections(bundle):
    bundle["artifacts"] = [
        {
            "section": section,
            "media_type": "application/json",
            "sha256": _digest(canonical_json_bytes(bundle[section])),
            "bytes": len(canonical_json_bytes(bundle[section])),
        }
        for section in bundle_sections(bundle)
    ]


def _reregister(result, content=None):
    """Repair outer checksums so negative tests reach the identity validation."""
    if content is None:
        _refresh_sections(result.bundle)
        content = canonical_json_bytes(result.bundle)
    result.artifact.sha256 = _digest(content)
    result.artifact.size_bytes = len(content)
    result.evaluation.payload["result_bundle"] = {
        "artifact_id": str(result.artifact.id),
        "sha256": result.artifact.sha256,
        "size_bytes": result.artifact.size_bytes,
    }
    result.evaluation.payload_sha256 = payload_hash(result.evaluation.payload)
    return replace(result, content=content)


def _validate(result):
    return validate_persisted_result(
        result.evaluation,
        result.run,
        result.dataset,
        result.artifact,
        result.content,
    )


@pytest.fixture
def persisted_native(exported, tmp_path):
    bundle = copy.deepcopy(exported[0])
    owner_id, dataset_id, strategy_id, listing_id = (uuid4() for _ in range(4))
    run_id, attempt_id, evaluation_id = (uuid4() for _ in range(3))
    inputs_root = tmp_path / "inputs"
    inputs_root.mkdir()
    seed = (ROOT / "seed_strategies/us_time.py").read_bytes()
    data = (FIXTURE / "US/INDEX.csv").read_bytes()
    (inputs_root / "strategy.py").write_bytes(seed)
    (inputs_root / "data.csv").write_bytes(data)
    manifest = {
        "files": {"data.csv": _digest(data)},
        "evaluator_symbol_mapping": {str(listing_id): "INDEX"},
        # Hostile text is inert metadata, not instructions or a real price basis.
        "price_semantics": {"fixture_note": UNSAFE_LABEL},
    }
    config = RunConfig(
        task="us_time",
        profile="corrected_explicit_config",
        dataset=VersionRef(
            id=str(dataset_id), sha256=_digest(canonical_bytes(manifest))
        ),
        seed_strategy=VersionRef(id=str(strategy_id), sha256=_digest(seed)),
        universe=VersionRef(id=str(uuid4()), sha256="b" * 64),
        windows=WindowSet(train=DateWindow(start="2024-02-02", end="2024-04-27")),
        warmup=WarmupConfig(sessions=20),
        market_profile=MarketConfig.for_market("US"),
        optimizer=OptimizerConfig(max_iterations=0),
        environment_digest="c" * 64,
    )
    window = evaluation_windows(config)[0]
    warmup = {
        "sessions": config.warmup.sessions,
        "history_policy": config.warmup.history_policy,
    }
    eval_config = {
        "engine_overrides": config.market_profile.engine_kwargs(),
        "window": {
            "start": window["start"],
            "end": window["end"],
            "role": window["split"],
        },
        "warmup": warmup,
        "benchmark": None,
        "run_config_sha256": config.sha256,
    }
    request = {
        "task": config.task,
        "market": config.market_profile.market,
        "profile": config.profile,
        "start_date": window["start"],
        "end_date": window["end"],
        "evaluation_config": eval_config,
    }
    (inputs_root / "request.json").write_bytes(canonical_bytes(request))
    inventory = artifact_inventory(inputs_root)
    bundle["identity"] = {
        "strategy_sha256": _digest(seed),
        "run_config_sha256": config.sha256,
        "evaluation_config_sha256": _digest(canonical_json_bytes(eval_config)),
        "input_manifest_sha256": _digest(canonical_json_bytes(inventory)),
        "inputs": inventory,
        "task": config.task,
        "market": config.market_profile.market,
        "profile": config.profile,
    }
    bundle["window"] = {
        **eval_config["window"],
        "interval": "half_open",
        "warmup": warmup,
        "engine_start_time": window["start"],
        "engine_end_time": str(date.fromisoformat(window["end"]) - timedelta(days=1)),
        "engine_boundary_adapter": "inclusive_daily_end_previous_calendar_date.v1",
    }
    bundle["decision_trace"]["input_snapshot_sha256"] = bundle["identity"][
        "input_manifest_sha256"
    ]
    # Never rewrite the engine's observed configuration to make the test pass.
    assert bundle["engine"]["requested_kwargs"] == {
        **config.market_profile.engine_kwargs(),
        "show_progress": False,
    }
    image_digest = "sha256:" + "d" * 64
    run = ResearchRun(
        id=run_id,
        owner_id=owner_id,
        dataset_version_id=dataset_id,
        strategy_version_id=strategy_id,
        config=config.model_dump(mode="json"),
        request_sha256=config.sha256,
        research_image_digest=image_digest,
        status="succeeded",
        generation=1,
    )
    dataset = DatasetVersion(
        id=dataset_id,
        owner_id=owner_id,
        name="Native projection test fixture",
        market="US",
        start_date=date(2024, 2, 2),
        end_date=date(2024, 4, 27),
        source="synthetic_fixture",
        is_synthetic=True,
        content_sha256=config.dataset.sha256,
        manifest=manifest,
        storage_key="inputs/data.csv",
    )
    evaluation = Evaluation(
        id=evaluation_id,
        run_id=run_id,
        attempt_id=attempt_id,
        generation=1,
        candidate_key="seed",
        code_sha256=_digest(seed),
        split="train",
        window=window,
        window_sha256=payload_hash(window),
        image_digest=image_digest,
        sandbox_stopped=True,
        status="succeeded",
        finished_at=datetime.now(UTC),
        payload={
            "status": "succeeded",
            "exit_code": 0,
            "image_digest": image_digest,
            "metrics": copy.deepcopy(bundle["legacy_performance"]["value"]),
        },
    )
    artifact = Artifact(
        id=uuid4(),
        run_id=run_id,
        attempt_id=attempt_id,
        name=f"evaluation-{evaluation_id}-result-bundle.json",
        media_type="application/json",
        storage_key="result-bundle.json",
    )
    result = _reregister(
        VerifiedResult(evaluation, run, dataset, artifact, bundle, b"")
    )
    (tmp_path / artifact.storage_key).write_bytes(result.content)
    content = read_verified(
        tmp_path, artifact.storage_key, artifact.sha256, MAX_WIRE_BYTES
    )
    # Deserialization, schema validation, and identity matching all use production
    # code. The returned bundle is reconstructed from disk, not the engine object.
    result = replace(
        result, content=content, bundle=_validate(replace(result, content=content))
    )
    return result, listing_id


def test_native_persisted_projection_preserves_precision_and_full_evidence(
    persisted_native, exported
):
    result, listing_id = persisted_native
    original_bytes = result.content
    full = result_view(
        result,
        sections=["equity", "drawdown", "fills", "closed_trades"],
        listing_id=listing_id,
        max_points=8,
        offset=1,
        limit=2,
    )
    for section in ("equity", "drawdown"):
        source, table = result.bundle[section]["rows"], full.tables[section]
        assert len(source) > 8
        assert table.returned_rows == len(table.rows) <= 8
        assert table.rows[0] == source[0]
        assert table.rows[-1] == source[-1]
        assert all(row in source for row in table.rows)
        assert min(row[1] for row in table.rows) == min(row[1] for row in source)
        assert max(row[1] for row in table.rows) == max(row[1] for row in source)
        assert table.downsampled and table.sampling == "min_max"
        assert table.filter_status == "portfolio_series_unfiltered_by_listing"
        assert table.total_rows == table.filtered_rows == len(source)
        assert (table.offset, table.limit) == (0, 8)
    for section in ("fills", "closed_trades"):
        source, table = result.bundle[section]["rows"], full.tables[section]
        assert len(source) >= 3
        assert table.rows == source[1:3]
        assert table.returned_rows == 2 and table.limit == 2 and table.offset == 1
        assert table.filter_status == "applied"
        assert not table.downsampled and table.sampling == "none"
    assert [metric.model_dump() for metric in full.metrics] == exported[0]["metrics"]
    precise = {metric.name: metric.value for metric in full.metrics}
    assert any(
        precise[name] != rounded
        for name, rounded in result.evaluation.payload["metrics"]["metrics"].items()
    )
    assert precise["combined_score"] == json.loads(exported[1])["combined_score"]
    assert full.capabilities.trusted_ledger is False
    assert full.benchmark.series is None
    assert full.fees.commission_stamp_transfer_breakdown is None
    # Querying never turns the sampled view into a replacement downloadable bundle.
    assert canonical_json_bytes(result.bundle) == original_bytes == result.content
    for section in (
        "engine",
        "metrics",
        "native_metrics",
        "equity",
        "drawdown",
        "cash",
        "margin",
        "positions",
        "orders",
        "fills",
        "closed_trades",
        "fees",
        "legacy_performance",
    ):
        assert result.bundle[section] == exported[0][section]


@pytest.mark.parametrize(
    "section,time_field", [("fills", "timestamp"), ("closed_trades", "exit_time")]
)
def test_native_table_date_filter_is_half_open_and_pages_after_filtering(
    persisted_native, section, time_field
):
    result, listing_id = persisted_native
    source = result.bundle[section]
    index = [column["name"] for column in source["columns"]].index(time_field)
    zone = ZoneInfo(result.bundle["engine"]["resolved_config"]["timezone"])
    days = [
        datetime.fromisoformat(row[index]).astimezone(zone).date()
        for row in source["rows"]
    ]
    start, end = min(days), max(days)
    assert start < end
    expected = [row for row, day in zip(source["rows"], days) if start <= day < end]
    table = result_view(
        result,
        sections=[section],
        listing_id=listing_id,
        start=start,
        end=end,
        offset=1,
        limit=2,
    ).tables[section]
    assert table.total_rows == len(source["rows"])
    assert table.filtered_rows == len(expected) < table.total_rows
    assert table.rows == expected[1:3]
    assert table.returned_rows == len(expected[1:3])


def test_native_report_is_escaped_script_free_and_does_not_round(persisted_native):
    result, _ = persisted_native
    before = result.content
    report = render_result_report(result)
    text = report.decode()
    assert len(report) <= MAX_REPORT_BYTES
    assert UNSAFE_LABEL not in text
    assert html.escape(json.dumps(UNSAFE_LABEL), quote=True) in text
    assert "Content-Security-Policy" in text
    assert "Synthetic demonstration data" in text
    assert result.artifact.sha256 in text and result.run.request_sha256 in text
    tags = []

    class Tags(HTMLParser):
        def handle_starttag(self, tag, attrs):
            tags.append((tag, attrs))

    Tags().feed(text)
    assert not {tag for tag, _ in tags} & {"script", "img", "iframe", "object", "link"}
    assert not any(name.startswith("on") for _, attrs in tags for name, _ in attrs)
    for metric in result.bundle["metrics"]:
        assert f"<td>{metric['value']}</td>" in text
    assert canonical_json_bytes(_validate(result)) == result.content == before


@pytest.mark.parametrize(
    "change",
    [
        "config",
        "evaluation_config",
        "request_hash",
        "request_bytes",
        "strategy_input",
        "boundary",
        "engine_kwargs",
    ],
)
def test_rehashed_native_bundle_still_requires_exact_pinned_identity(
    persisted_native, change
):
    result, _ = persisted_native
    identity = result.bundle["identity"]
    inputs = {entry["path"]: entry for entry in identity["inputs"]}
    if change == "config":
        identity["run_config_sha256"] = "0" * 64
    elif change == "evaluation_config":
        identity["evaluation_config_sha256"] = "0" * 64
    elif change == "request_hash":
        inputs["request.json"]["sha256"] = "0" * 64
    elif change == "request_bytes":
        inputs["request.json"]["bytes"] += 1
    elif change == "strategy_input":
        inputs["strategy.py"]["sha256"] = "0" * 64
    elif change == "boundary":
        result.bundle["window"]["engine_end_time"] = result.bundle["window"]["end"]
    elif change == "engine_kwargs":
        result.bundle["engine"]["requested_kwargs"]["commission_rate"] = 0.002
    identity["input_manifest_sha256"] = _digest(
        canonical_json_bytes(identity["inputs"])
    )
    result.bundle["decision_trace"]["input_snapshot_sha256"] = identity[
        "input_manifest_sha256"
    ]
    result = _reregister(result)
    validate_result_bundle(result.bundle)  # All schema/section/outer hashes agree.
    with pytest.raises(DomainError) as caught:
        _validate(result)
    assert (caught.value.code, caught.value.status_code) == ("result_integrity", 409)


@pytest.mark.parametrize(
    "change",
    [
        "code",
        "dataset",
        "dataset_manifest",
        "run",
        "unfinished",
        "artifact_attempt",
        "payload_metrics",
    ],
)
def test_native_registration_mismatch_is_rejected(persisted_native, change):
    result, _ = persisted_native
    if change == "code":
        result.evaluation.code_sha256 = "0" * 64
    elif change == "dataset":
        result.dataset.content_sha256 = "0" * 64
    elif change == "dataset_manifest":
        result.dataset.manifest["price_semantics"]["fixture_note"] = "tampered"
    elif change == "run":
        result.run.request_sha256 = "0" * 64
    elif change == "unfinished":
        result.evaluation.sandbox_stopped = False
    elif change == "artifact_attempt":
        result.artifact.attempt_id = uuid4()
    elif change == "payload_metrics":
        result.evaluation.payload["metrics"]["combined_score"] += 1
        result.evaluation.payload_sha256 = payload_hash(result.evaluation.payload)
    with pytest.raises(DomainError) as caught:
        _validate(result)
    assert (caught.value.code, caught.value.status_code) == ("result_integrity", 409)


def test_noncanonical_native_bytes_rejected_even_with_matching_registration(
    persisted_native,
):
    result, _ = persisted_native
    noncanonical = json.dumps(result.bundle, indent=2, allow_nan=False).encode()
    assert json.loads(noncanonical) == result.bundle
    result = _reregister(result, content=noncanonical)
    with pytest.raises(DomainError) as caught:
        _validate(result)
    assert (caught.value.code, caught.value.status_code) == ("result_integrity", 409)
