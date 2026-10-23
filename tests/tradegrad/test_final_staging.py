"""Offline final-window bytes only; candidate source is never executed.

Synthetic daily rows are staging evidence, not exchange-calendar or engine
acceptance. These tests reuse production hashing, filtering and immutable writes.
"""

import copy
import csv
import hashlib
import io
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.experiment_schemas import SplitDefinition
from backend.app.services import DomainError
from packages.tradegrad_adapter.config import MarketConfig, RunConfig, canonical_bytes
from packages.tradegrad_adapter.environment import environment_digest
from packages.tradegrad_adapter.final_config import FinalEvaluationConfig
from packages.tradegrad_adapter.windows import PreflightError
from workers.research.final_staging import stage_final_input
from workers.research.optimization import stage_research_input

ROOT = Path(__file__).resolve().parents[2]
CODE = 'raise RuntimeError("Final candidate must never execute on the host")\n'
IMAGE = "sha256:" + "a" * 64


def sha(value):
    return hashlib.sha256(value).hexdigest()


def final_inputs(root, *, task="cn_time"):
    days = [date(2024, 1, 1) + timedelta(days=index) for index in range(70)]
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["date", "open", "high", "low", "close", "volume"])
    writer.writerows([str(day), 10, 11, 9, 10, 100] for day in days)
    files = {
        "data/SOURCE.csv": stream.getvalue().encode(),
        "data/FUTURE.csv": stream.getvalue().encode(),
        "universe/members_list_2024-01-01.csv": b"updateDate,code,code_name\n2024-01-01,SOURCE,Synthetic\n",
        "universe/members_list_2024-03-01.csv": b"updateDate,code,code_name\n2024-03-01,FUTURE,Outside final window\n",
    }
    universe_id, universe_sha = str(uuid4()), sha(b"synthetic final universe")
    manifest = {
        "files": {name: sha(value) for name, value in files.items()},
        "universe": {
            "id": universe_id,
            "content_sha256": universe_sha,
            "state": "historical_membership",
        },
        "evaluator_symbol_mapping": {
            "source-listing": "SOURCE",
            "future-listing": "FUTURE",
        },
        "listings": [
            {"listing_id": "source-listing"},
            {"listing_id": "future-listing"},
        ],
        "is_synthetic": True,
    }
    source = RunConfig.model_validate(
        {
            "execution_mode": "optimization",
            "task": task,
            "dataset": {"id": str(uuid4()), "sha256": sha(canonical_bytes(manifest))},
            "seed_strategy": {
                "id": str(uuid4()),
                "sha256": sha(b"different seed source"),
            },
            "universe": {"id": universe_id, "sha256": universe_sha},
            "windows": {
                "train": {"start": "2024-01-22", "end": "2024-02-01"},
                "validation": {"start": "2024-02-03", "end": "2024-02-10"},
                "holdout": {"start": "2024-02-15", "end": "2024-02-20"},
            },
            "warmup": {"sessions": 20 if task.endswith("_time") else 3},
            "market_profile": MarketConfig.for_market("CN").model_dump(mode="json"),
            "optimizer": {"max_iterations": 1},
            "environment_digest": environment_digest(),
        }
    )
    split = SplitDefinition(
        dataset=source.dataset, universe=source.universe, windows=source.windows
    )
    final = FinalEvaluationConfig(
        selection_id=uuid4(),
        split_version_id=uuid4(),
        split_sha256=split.sha256,
        selected_strategy={"id": str(uuid4()), "sha256": sha(CODE.encode())},
        source_config=source,
        source_config_sha256=source.sha256,
        holdout=source.windows.holdout,
        environment_digest=source.environment_digest,
        max_runtime_seconds=30,
    )
    for name, content in files.items():
        path = root / "datasets/fixture" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return final, manifest, days


def stage(root, config, manifest, *, code=CODE, target=None):
    return stage_final_input(
        config=config,
        manifest=manifest,
        storage_root=root,
        dataset_storage_key="datasets/fixture",
        code=code,
        target=target or root / "attempt/input",
        max_bytes=1_000_000,
    )


def test_final_imports_no_optimizer_model_or_engine_runtime():
    program = (
        "import sys; import workers.research.final_evaluation; "
        "assert not set(('numpy', 'openai', 'akquant', 'openbb', 'src.optimizer', "
        "'packages.tradegrad_adapter.optimizer', 'packages.tradegrad_adapter.model_calls')) & set(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("task", ["cn_time", "cn_cross"])
def test_final_stages_only_exact_holdout_and_declared_warmup(tmp_path, task):
    config, manifest, days = final_inputs(tmp_path, task=task)
    target = stage(tmp_path, config, manifest)
    name = "INDEX.csv" if task.endswith("_time") else "SOURCE.csv"
    with (target / "data" / name).open() as stream:
        rows = list(csv.DictReader(stream))
    expected = [day for day in days if day < config.holdout.start][
        -config.source_config.warmup.sessions :
    ]
    expected += [
        day for day in days if config.holdout.start <= day < config.holdout.end
    ]
    assert [row["date"] for row in rows] == list(map(str, expected))
    assert not (target / "data/FUTURE.csv").exists()
    assert not (target / "universe/members_list_2024-03-01.csv").exists()
    assert (target / "strategy.py").read_text() == CODE
    assert config.selected_strategy.sha256 != config.source_config.seed_strategy.sha256
    request = json.loads((target / "request.json").read_bytes())
    assert request["start_date"] == str(config.holdout.start)
    assert request["end_date"] == str(config.holdout.end)
    assert request["evaluation_config"]["run_config_sha256"] == config.sha256
    assert request["evaluation_config"]["window"] == {
        "start": str(config.holdout.start),
        "end": str(config.holdout.end),
        "role": "holdout",
    }
    report = json.loads((target.parent / "window-report.json").read_bytes())
    assert report["window"] == config.holdout.model_dump(mode="json")
    assert (
        report["listings"][0]["staged_warmup_sessions"]
        == config.source_config.warmup.sessions
    )
    assert not report["point_in_time_proven"]
    files = {
        str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()
    }
    assert files == {
        f"data/{name}",
        "strategy.py",
        "request.json",
        "universe/members_list_2024-01-01.csv",
    }
    assert all(not path.stat().st_mode & 0o222 for path in target.rglob("*"))
    assert not target.stat().st_mode & 0o222


@pytest.mark.parametrize(
    "change",
    ["source", "empty_source", "large_source", "manifest", "data", "config", "warmup"],
)
def test_final_rejects_tampered_input_before_creating_mount(tmp_path, change):
    config, manifest, _ = final_inputs(tmp_path)
    code = CODE
    if change == "source":
        code += "# changed\n"
    elif change == "empty_source":
        code = ""
    elif change == "large_source":
        code = "#" * 1_048_577
    elif change == "manifest":
        manifest["universe"]["state"] = "user_fixed"
    elif change == "data":
        (tmp_path / "datasets/fixture/data/SOURCE.csv").write_text("changed")
    elif change == "config":
        config = config.model_copy(update={"environment_digest": "0" * 64})
    else:
        raw = copy.deepcopy(config.source_config.model_dump(mode="json"))
        raw["warmup"]["sessions"] = 21
        config = config.model_copy(
            update={"source_config": RunConfig.model_validate(raw)}
        )
    with pytest.raises((ValueError, ValidationError, DomainError, PreflightError)):
        stage(tmp_path, config, manifest, code=code)
    assert not (tmp_path / "attempt/input").exists()


def test_final_never_reuses_a_physical_input_namespace(tmp_path):
    config, manifest, _ = final_inputs(tmp_path)
    target = stage(tmp_path, config, manifest)
    with pytest.raises(FileExistsError):
        stage(tmp_path, config, manifest)
    assert (target / "strategy.py").read_text() == CODE


def test_old_optimizer_staging_still_cannot_accept_holdout(tmp_path):
    config, manifest, _ = final_inputs(tmp_path)
    with pytest.raises(PreflightError):
        stage_research_input(
            config=config.source_config,
            manifest=manifest,
            storage_root=tmp_path,
            dataset_storage_key="datasets/fixture",
            code=CODE,
            window={"split": "holdout", **config.holdout.model_dump(mode="json")},
            target=tmp_path / "optimizer/input",
            max_bytes=1_000_000,
        )
    assert not (tmp_path / "optimizer/input").exists()
