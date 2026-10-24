"""Actual native browser-fixture regression, not PostgreSQL or browser admission."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from packages.research_trace.contracts import CaptureConfig
from packages.research_trace.diagnostic_parity import compare_diagnostic
from packages.research_trace.transport import (
    TRACE_PREFIX,
    CaptureReceiver,
    iter_capture_records,
)
from tests.research_trace.test_diagnostic_parity import inputs
from tests.research_trace.test_native_observation_entrypoint import decoded, normalized
from workers.research.diagnostic_parity_publication import _ALLOWED_CAPTURE_GAPS

ROOT = Path(__file__).resolve().parents[2]


def execute(
    path, *, window, independent=False, restore=None, capture="research", split="train"
):
    python = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON")
    if not python:
        if os.environ.get("TRADEGRAD_NATIVE_TEST_REQUIRED") == "1":
            pytest.fail("Browser-fixture parity requires the pinned native interpreter")
        pytest.skip("Actual native browser-fixture parity is opt-in")
    process = subprocess.run(
        [python, str(ROOT / "tests/research_trace/native_parity_fixture.py")],
        input=json.dumps(
            {
                "window": window,
                "independent": independent,
                "restore": restore,
                "capture": capture,
                "split": split,
            }
        ).encode(),
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "TRADEGRAD_DATA_WORKERS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        },
        capture_output=True,
        timeout=90,
        check=False,
    )
    envelope, observation = decoded(process)
    config = CaptureConfig(mode=capture)
    path.mkdir()
    receiver = CaptureReceiver(path, config, source="strategy_emitted")
    for line in process.stdout.splitlines():
        if line.startswith(TRACE_PREFIX.encode()):
            receiver.accept(json.loads(line[len(TRACE_PREFIX) :]))
    transport = receiver.result()
    records = list(
        iter_capture_records(
            transport["research_capture_path"],
            transport["research_capture_sha256"],
            transport["research_capture_size_bytes"],
            config,
        )
    )
    return {
        "bundle": envelope["bundle"],
        "observation": observation,
        "records": [record.model_dump(mode="json") for record in records],
        "capture": config.model_dump(mode="json"),
        "gaps": tuple(
            reason
            for reason in transport["research_capture"]["missing_reasons"]
            if reason not in _ALLOWED_CAPTURE_GAPS
        ),
    }


@pytest.mark.parametrize("window,count", [("original", 25), ("paging", 26)])
def test_actual_browser_fixture_full_capture_parity_keeps_submission_origins(
    tmp_path, window, count
):
    original = execute(tmp_path / "original", window=window)
    replay = execute(
        tmp_path / "replay",
        window=window,
        independent=True,
        restore=original["observation"]["rng"]["state"],
    )
    off = execute(tmp_path / "off", window=window, capture="off")
    assert normalized(original["bundle"]) == normalized(off["bundle"])
    assert original["bundle"]["native_metrics"] == off["bundle"]["native_metrics"]
    assert original["bundle"]["fees"] == off["bundle"]["fees"]
    assert original["bundle"]["orders"]["rows"]
    assert original["bundle"]["fills"]["rows"]
    shared = {
        "dataset_version_id": uuid4(),
        "dataset_sha256": "1" * 64,
        "universe_version_id": uuid4(),
        "universe_sha256": "2" * 64,
        "engine_dependencies_sha256": "3" * 64,
    }
    # These synthetic business pins are not DB admission. Crucially, unlike the
    # old short direct-engine fixture, real transport gaps participate in parity.
    sides = [
        replace(
            inputs(value, shared, name="daily_timing"),
            capture=value["capture"],
            capture_missing=value["gaps"],
        )
        for value in (original, replay)
    ]
    report = compare_diagnostic(*sides)
    assert report.outcome == "matched", [
        (c.key, c.status, c.reason) for c in report.checks
    ]
    assert not original["gaps"] and not replay["gaps"]
    assert report.original_event_count == report.replay_event_count == count
    assert len(report.events) == count
    assert all(event.mapping == "verified_original_event" for event in report.events)
    assert all(
        event.original_decision_id != event.replay_decision_id
        for event in report.events
    )
    if window == "paging":
        assert count > 25
        # Extending train never makes the remaining real optimizer leaf invalid.
        validation = execute(tmp_path / "validation", window=window, split="validation")
        assert validation["bundle"]["fills"]["rows"]
        assert not validation["gaps"]
