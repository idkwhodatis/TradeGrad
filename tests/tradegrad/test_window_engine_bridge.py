"""Opt-in LOCAL component bridge: staged research windows -> original AKQuant -> V2.

    TRADEGRAD_LOCAL_ENGINE_BRIDGE=1 .venv-research/bin/python -m pytest \
        tests/tradegrad/test_window_engine_bridge.py -q

The API/optimizer runtime must not contain AKQuant. Only the four original seeds
whose SHA-256 values were verified at SOURCE_COMMIT may execute, in the existing
locked .venv engine interpreter. No generated/modified candidate is executed.
The unchanged synthetic baseline CSVs cover only Jan-Apr 2024: this exercises the
canonical 2024 yearly-train slice and full validation, not a cross-year dataset.
This is NOT Docker/sandbox, PostgreSQL, HTTP, live-data, optimizer-orchestration,
exchange-calendar, independently trusted ledger, or investment-quality evidence.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from packages.tradegrad_adapter.config import (
    MarketConfig,
    RunConfig,
    canonical_bytes,
    evaluation_windows,
)
from packages.tradegrad_adapter.environment import environment_digest
from packages.tradegrad_adapter.results import RESULT_PREFIX_V2, canonical_json_bytes
from runtimes.evaluator.runner import decode_result_protocol
from src.job import evaluator_environment

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/baseline/fixtures"
ENGINE_PYTHON = ROOT / ".venv/bin/python"
SOURCE_COMMIT = "baf73ee56353de8996731139f6721b62f4b26da7"
SEED_HASHES = {
    "cn_time": "09600e5e14cb293dfd0411b697d4ff95774c045845884620d4686f11485401ba",
    "cn_cross": "486e13c83d179a1a9ba3e9d79614bdff75b001f34c470ce848b4da59b167a78e",
    "us_time": "ecdcc54ffe62c05db0220d6e3e5a48aab10145b7e34b3e0a5aa84e33f4d149e8",
    "us_cross": "3621b9051b95aca0bca650fdc607acfa223c2fee18b47ccb4069367429a91b93",
}
WINDOWS = (
    {"split": "train", "start": "2024-02-02", "end": "2024-03-15"},
    {"split": "validation", "start": "2024-03-15", "end": "2024-04-12"},
)
HOLDOUT = {"start": "2024-04-12", "end": "2024-04-27"}
MAX_INPUT_BYTES = 1_048_576
MAX_STDOUT_BYTES = 16 * 1_048_576

pytestmark = pytest.mark.skipif(
    os.environ.get("TRADEGRAD_LOCAL_ENGINE_BRIDGE") != "1",
    reason="Local original-only bridge is opt-in; it cannot replace PG/Docker gates",
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def original_seed(task: str) -> bytes:
    """No configurable strategy path or dynamically accepted candidate digest."""
    assert task in SEED_HASHES
    path = ROOT / "seed_strategies" / f"{task}.py"
    assert path.is_file() and not path.is_symlink()
    content = path.read_bytes()
    assert sha(content) == SEED_HASHES[task], (
        f"Refusing host execution: {task} differs from original {SOURCE_COMMIT}"
    )
    return content


def csv_rows(content: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content.decode(), newline="")))


def inventory(root: Path) -> list[dict]:
    # Independently reconstruct the wire identity rather than trusting the exporter.
    return [
        {
            "path": p.relative_to(root).as_posix(),
            "sha256": sha(p.read_bytes()),
            "bytes": p.stat().st_size,
        }
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]


@pytest.fixture(scope="module")
def real_engine():
    if any(importlib.util.find_spec(name) is None for name in ("sqlalchemy", "openai")):
        pytest.skip("Requires the existing .venv-research API/optimizer runtime")
    assert importlib.util.find_spec("akquant") is None, (
        "Run from .venv-research, not the engine runtime; coordinator must lack AKQuant"
    )
    if not ENGINE_PYTHON.is_file():
        pytest.skip("Existing .venv engine interpreter is unavailable")
    probe = subprocess.run(
        [
            str(ENGINE_PYTHON),
            "-B",
            "-c",
            (
                "import importlib.util as u, importlib.metadata as m, json; "
                "s=u.find_spec('akquant'); print(json.dumps(None if s is None else "
                "{'version':m.version('akquant'),'module':s.origin}))"
            ),
        ],
        cwd=ROOT,
        env=evaluator_environment(),
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    actual = json.loads(probe.stdout)
    if actual is None:
        pytest.skip(
            "Existing .venv has no AKQuant; no installation or fallback is attempted"
        )
    assert actual["version"] == "0.3.27"
    assert (ROOT / ".venv").resolve() in Path(actual["module"]).resolve().parents
    return actual


def immutable_fixture(root: Path, task: str):
    """Use verified baseline OHLCV bytes and synthetic, explicitly dated membership."""
    source_manifest = (FIXTURE / "manifest.json").read_bytes()
    baseline = json.loads(source_manifest)
    assert baseline["source_commit"] == SOURCE_COMMIT
    market = task[:2].upper()
    source_bytes = {"manifest.json": source_manifest}

    def frozen(name):
        path = FIXTURE / name
        assert path.is_file() and not path.is_symlink()
        content = path.read_bytes()
        assert len(content) <= MAX_INPUT_BYTES
        assert sha(content) == baseline["files"][name]
        source_bytes[name] = content
        return content

    if task.endswith("_time"):
        # Deliberately not named INDEX: staging, not the test, must normalize it.
        symbols = ["ORIGINAL_INDEX_SOURCE"]
        files = {f"data/{symbols[0]}.csv": frozen(f"{market}/INDEX.csv")}
        membership = (
            b"updateDate,code,code_name\n"
            b"2024-01-02,ORIGINAL_INDEX_SOURCE,Synthetic baseline timing fixture\n"
        )
    else:
        membership = frozen(f"{market}_list/fixture_list_2024-01.csv")
        symbols = [row["code"] for row in csv_rows(membership)]
        assert len(symbols) == 40
        files = {f"data/{s}.csv": frozen(f"{market}/{s}.csv") for s in symbols}
    files["universe/fixture_list_2024-01.csv"] = membership
    # The fixture contains actual rows at/after both window ends and a future-only
    # member. Neither its membership nor its data may leak into train/validation.
    files["data/HOLDOUT_ONLY.csv"] = frozen(f"{market}/INDEX.csv")
    files["universe/holdout_list_2024-04-12.csv"] = (
        b"updateDate,code,code_name\n2024-04-12,HOLDOUT_ONLY,Synthetic future member\n"
    )
    universe_sha = sha(
        canonical_bytes(
            {
                name: sha(content)
                for name, content in files.items()
                if name.startswith("universe/")
            }
        )
    )
    manifest = {
        "files": {name: sha(content) for name, content in files.items()},
        "universe": {
            "id": f"{task}-fixture-universe",
            "content_sha256": universe_sha,
            "state": "historical_membership",
        },
        "evaluator_symbol_mapping": {
            f"fixture-{s}": s for s in [*symbols, "HOLDOUT_ONLY"]
        },
        "listings": [
            {"listing_id": f"fixture-{s}"} for s in [*symbols, "HOLDOUT_ONLY"]
        ],
    }
    code = original_seed(task)
    config = RunConfig.model_validate(
        {
            "execution_mode": "optimization",
            "task": task,
            "dataset": {
                "id": f"{task}-baseline",
                "sha256": sha(canonical_bytes(manifest)),
            },
            "seed_strategy": {"id": f"{task}-original", "sha256": SEED_HASHES[task]},
            "universe": {"id": f"{task}-fixture-universe", "sha256": universe_sha},
            "windows": {
                **{w["split"]: {"start": w["start"], "end": w["end"]} for w in WINDOWS},
                "holdout": HOLDOUT,
            },
            "warmup": {"sessions": 20 if task.endswith("_time") else 3},
            "market_profile": MarketConfig.for_market(market).model_dump(mode="json"),
            "budgets": {
                "max_model_calls": 0,
                "max_tokens": 0,
                "max_runtime_seconds": 60,
            },
            "environment_digest": environment_digest(),
        }
    )
    dataset = root / "datasets/fixture"
    for name, content in files.items():
        path = dataset / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o444)
    (dataset / "manifest.json").write_bytes(canonical_bytes(manifest))
    (dataset / "manifest.json").chmod(0o444)
    config_path = root / "run-config.json"
    config_path.write_bytes(config.canonical_bytes())
    config_path.chmod(0o444)
    config = RunConfig.from_verified_json(config_path.read_bytes(), config.sha256)
    return config, manifest, files, source_bytes, code


@pytest.mark.parametrize("task", tuple(SEED_HASHES))
@pytest.mark.parametrize(
    "window_index", [0, 1], ids=["yearly-train", "full-validation"]
)
def test_real_staging_original_entrypoint_and_v2_decoder(
    tmp_path,
    real_engine,
    task,
    window_index,
):
    from workers.research.optimization import stage_research_input

    config, manifest, files, sources, code = immutable_fixture(tmp_path, task)
    config_bytes, manifest_bytes = config.canonical_bytes(), canonical_bytes(manifest)
    assert evaluation_windows(config) == WINDOWS
    assert all(w["split"] != "holdout" for w in evaluation_windows(config))
    window = evaluation_windows(config)[window_index]
    input_dir = stage_research_input(
        config=config,
        manifest=manifest,
        storage_root=tmp_path,
        dataset_storage_key="datasets/fixture",
        code=code.decode(),
        window=window,
        target=tmp_path / "attempt/input",
        max_bytes=MAX_INPUT_BYTES,
    )
    request = json.loads((input_dir / "request.json").read_bytes())
    start, end = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
    expected_config = {
        "engine_overrides": config.market_profile.engine_kwargs(),
        "window": {"start": str(start), "end": str(end), "role": window["split"]},
        "warmup": {"sessions": config.warmup.sessions, "history_policy": "require"},
        "benchmark": None,
        "run_config_sha256": config.sha256,
    }
    assert request == {
        "task": task,
        "market": task[:2].upper(),
        "profile": "corrected_explicit_config",
        "start_date": str(start),
        "end_date": str(end),
        "evaluation_config": expected_config,
    }
    report = json.loads((input_dir.parent / "window-report.json").read_bytes())
    assert report["window"] == {"start": str(start), "end": str(end)}
    assert (
        report["split"] == window["split"] and report["point_in_time_proven"] is False
    )
    reports = {r["listing_id"]: r for r in report["listings"]}
    staged_names = set()
    for listing_id, symbol in manifest["evaluator_symbol_mapping"].items():
        if symbol == "HOLDOUT_ONLY":
            assert listing_id not in reports
            continue
        source = files[f"data/{symbol}.csv"]
        rows = csv_rows(source)
        previous = [r for r in rows if r["date"] < str(start)][
            -config.warmup.sessions :
        ]
        active = [r for r in rows if str(start) <= r["date"] < str(end)]
        assert len(previous) == config.warmup.sessions
        assert any(r["date"] == str(end) for r in rows), "Exercise the exclusive end"
        assert any(r["date"] >= HOLDOUT["start"] for r in rows)
        name = "data/INDEX.csv" if task.endswith("_time") else f"data/{symbol}.csv"
        staged_names.add(name)
        staged = (input_dir / name).read_bytes()
        assert csv_rows(staged) == previous + active
        assert all(
            r["date"] < min(str(end), HOLDOUT["start"]) for r in csv_rows(staged)
        )
        row_report = reports[listing_id]
        assert row_report["source_sha256"] == sha(source)
        assert row_report["staged_sha256"] == sha(staged)
        assert row_report["required_warmup_sessions"] == config.warmup.sessions
        assert row_report["staged_warmup_sessions"] == config.warmup.sessions
        assert row_report["evaluation_rows"] == len(active)
        assert row_report["expected_session_check"] == "unavailable"
    assert {p.relative_to(input_dir).as_posix() for p in input_dir.rglob("*.csv")} == {
        *staged_names,
        "universe/members_list_2024-01-02.csv",
    }
    assert (input_dir / "strategy.py").read_bytes() == code == original_seed(task)
    assert all(not p.stat().st_mode & 0o222 for p in [input_dir, *input_dir.rglob("*")])
    before = inventory(input_dir)
    expected_identity = {
        "strategy_sha256": SEED_HASHES[task],
        "run_config_sha256": config.sha256,
        "evaluation_config_sha256": sha(canonical_json_bytes(expected_config)),
        "input_manifest_sha256": sha(canonical_json_bytes(before)),
        "inputs": before,
        "task": task,
        "market": task[:2].upper(),
        "profile": "corrected_explicit_config",
    }
    native_output, decoded_output = (
        tmp_path / "native-output",
        tmp_path / "decoded-output",
    )
    stdout, stderr = tmp_path / "engine.stdout", tmp_path / "engine.stderr"
    # Check the pinned copied seed again inside the child immediately before main.
    # The entrypoint is real; no fake engine, modified strategy, PG or Docker call.
    script = (
        "import hashlib, sys; from pathlib import Path; "
        f"pins={SEED_HASHES!r}; "
        "p=Path(sys.argv[1]); s=p/'strategy.py'; "
        "assert not s.is_symlink() and hashlib.sha256(s.read_bytes()).hexdigest()==pins[sys.argv[3]]; "
        "from runtimes.evaluator.entrypoint import main; main(p,Path(sys.argv[2]))"
    )
    with stdout.open("wb") as out, stderr.open("wb") as err:
        completed = subprocess.run(
            [
                str(ENGINE_PYTHON),
                "-B",
                "-c",
                script,
                str(input_dir),
                str(native_output),
                task,
            ],
            cwd=ROOT,
            env=evaluator_environment({"AKQUANT_DATA_CACHE": str(tmp_path / "cache")}),
            stdout=out,
            stderr=err,
            timeout=60,
            check=False,
        )
    assert completed.returncode == 0, stderr.read_bytes()[-8192:].decode(
        errors="replace"
    )
    assert stdout.stat().st_size <= MAX_STDOUT_BYTES
    wire = stdout.read_bytes()
    records = [
        line for line in wire.splitlines() if line.startswith(b"TRADEGRAD_RESULT_")
    ]
    assert len(records) == 1 and records[0].startswith(RESULT_PREFIX_V2.encode())
    decoded_output.mkdir()
    metrics, bundle = decode_result_protocol(
        wire,
        decoded_output,
        expected_config_sha256=config.sha256,
        expected_identity=expected_identity,
    )
    assert bundle is not None and bundle["identity"] == expected_identity
    assert metrics == json.loads((native_output / "metrics.json").read_bytes())
    assert metrics["market"] == task[:2].upper()
    assert metrics["profile"] == "corrected_explicit_config"
    assert metrics["metrics"]["trade_per_day"] > 0
    assert (decoded_output / "metrics.json").read_bytes() == canonical_json_bytes(
        metrics
    )
    assert (decoded_output / "result-bundle.json").read_bytes() == canonical_json_bytes(
        bundle
    )
    assert bundle["window"] == {
        **expected_config["window"],
        "interval": "half_open",
        "warmup": expected_config["warmup"],
        "engine_start_time": str(start),
        "engine_end_time": str(end - timedelta(days=1)),
        "engine_boundary_adapter": "inclusive_daily_end_previous_calendar_date.v1",
    }
    engine = bundle["engine"]
    assert engine["name"] == "akquant" and engine["version"] == real_engine["version"]
    assert engine["currency"] == config.market_profile.currency
    assert engine["requested_kwargs"] == {
        **config.market_profile.engine_kwargs(),
        "show_progress": False,
    }
    assert engine["resolved_config"]["timezone"] == config.market_profile.timezone
    timezone = ZoneInfo(config.market_profile.timezone)
    for section, field in (
        ("orders", "created_at"),
        ("fills", "timestamp"),
        ("equity", "timestamp"),
    ):
        table = bundle[section]
        assert table["rows"], (
            f"No {section}: pre-start assertion must not pass vacuously"
        )
        index = [column["name"] for column in table["columns"]].index(field)
        timestamps = [datetime.fromisoformat(row[index]) for row in table["rows"]]
        assert all(
            t.tzinfo is not None and start <= t.astimezone(timezone).date() < end
            for t in timestamps
        )
    for artifact in bundle["artifacts"]:
        content = canonical_json_bytes(bundle[artifact["section"]])
        assert artifact["sha256"] == sha(content) and artifact["bytes"] == len(content)
    assert bundle["capabilities"]["trusted_ledger"] is False
    assert bundle["model_usage"]["calls"] == []
    assert bundle["benchmark"]["status"] == "not_configured"
    assert inventory(input_dir) == before
    assert (
        config.canonical_bytes()
        == (tmp_path / "run-config.json").read_bytes()
        == config_bytes
    )
    assert (
        canonical_bytes(manifest)
        == (tmp_path / "datasets/fixture/manifest.json").read_bytes()
        == manifest_bytes
    )
    assert original_seed(task) == code
    for name, content in files.items():
        assert (tmp_path / "datasets/fixture" / name).read_bytes() == content
    for name, content in sources.items():
        assert (FIXTURE / name).read_bytes() == content
    assert "akquant" not in sys.modules, "The coordinator must never import the engine"
