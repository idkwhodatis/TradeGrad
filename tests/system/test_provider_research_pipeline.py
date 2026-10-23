"""Opt-in real PG/queue/Docker adapter → same snapshot → TradeTGD gate.

Run only on explicitly authorized hosted infrastructure with
TRADEGRAD_REQUIRE_PROVIDER_RESEARCH=1, TEST_DATABASE_URL and the three dedicated
API_PYTHON/RESEARCH_PYTHON/DATA_PYTHON runtimes. Collection opens no connections.
SDK responses are deterministic generated synthetic rows, not live data. The
unchanged TradeTGD optimizer runs one frozen-model iteration. No uploaded market
fixture, fake queue, simulated runner or paid model is substituted.
"""

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_research_optimization import (
    ResearchProcessHarness,
    research_rows,
    runtime_paths,
)
from test_worker_recovery import ROOT, require

from backend.app.storage import safe_path
from packages.tradegrad_adapter.config import (
    MarketConfig,
    RunConfig,
    evaluation_windows,
)
from packages.tradegrad_adapter.results import validate_result_bundle

SCENARIOS = [
    ("akshare", ["600000", "000001"], "cn_cross", "unadjusted", "unadjusted"),
    ("yfinance", ["AAPL"], "us_time", "adjusted", "unadjusted"),
    ("openbb", ["MSFT"], "us_time", "unknown", "unknown"),
]
START, END = "2023-07-03", "2024-01-02"


@pytest.fixture
def provider_harness(tmp_path, request):
    if os.getenv("TRADEGRAD_REQUIRE_PROVIDER_RESEARCH") != "1":
        pytest.skip("Real provider-to-research acceptance was not explicitly requested")
    url = os.getenv("TEST_DATABASE_URL")
    require(bool(url), "TEST_DATABASE_URL is mandatory")
    harness = None
    try:
        harness = ResearchProcessHarness(tmp_path, url, runtime_paths())
        harness.deadline = time.monotonic() + 360
        harness.setup()
        if getattr(request, "param", None) != "deferred-data-worker":
            for role in ("dispatcher", "worker"):
                harness.spawn(f"data-{role}", ["-m", "workers.data", role])
        yield harness
    finally:
        if harness is not None:
            harness.cleanup()


def wait_import(h, job):
    def ready():
        value = h.request(h.clients[0], "GET", f"/data/imports/{job['id']}").json()
        require(
            value["status"] not in {"failed", "partial", "cancelled"},
            "Actual adapter import did not complete",
        )
        return value if value["status"] == "ready" else None

    return h.until(ready, "Actual data queue worker did not complete", timeout=80)


def fingerprint_tree(path):
    return {
        str(file.relative_to(path)): (
            hashlib.sha256(file.read_bytes()).hexdigest(),
            file.stat().st_mtime_ns,
        )
        for file in path.rglob("*")
        if file.is_file()
    }


@pytest.mark.parametrize("provider,symbols,task,split,dividend", SCENARIOS)
def test_actual_provider_download_to_corrected_optimizer_and_persisted_result(
    provider_harness, provider, symbols, task, split, dividend
):
    h = provider_harness
    client, other = h.clients
    job = h.request(
        client,
        "POST",
        "/data/imports",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "name": f"Generated {provider} SDK research fixture, NOT live",
            "provider": provider,
            "market": task[:2].upper(),
            "symbols": symbols,
            "start_date": START,
            "end_date": END,
            "price_mode": "raw" if provider == "akshare" else "provider_native",
        },
    ).json()
    ready = wait_import(h, job)
    dataset = h.request(
        client, "GET", f"/datasets/{ready['dataset_version_id']}"
    ).json()
    manifest = dataset["manifest"]
    binding = job["request"].get("_provider_connection")
    require(
        binding is not None
        and binding["provider"] == provider
        and binding["revision"] >= 1
        and binding["auth_mode"] == "public_no_auth"
        and manifest.get("provider_connection") == binding,
        "Actual worker omitted or changed frozen provider connection evidence",
    )
    require(
        job["request"]["_execution"]["worker_contract"] == "data-import.v3",
        "Provider connection job did not use the fail-closed worker contract",
    )
    require(
        dataset["source"] == provider and dataset["is_synthetic"],
        "Provider fixture mislabeled",
    )
    require(
        manifest["price_semantics"]["split_basis"] == split
        and manifest["price_semantics"]["dividend_basis"] == dividend,
        "Provider price semantics changed",
    )
    require(
        all(
            p["evidence"] == "fixture" and p.get("terms_url")
            for p in manifest["provenance"]
        ),
        "Fixture/terms provenance missing",
    )
    require(
        manifest["import_execution_policy"]["fixture_version"] == "synthetic-daily.v3",
        "Wrong fixture generation",
    )
    # Read-only SQL obtains the internal storage key, not an invented fixture file.
    with h.connection(private=True) as connection:
        key = connection.execute(
            "SELECT storage_key FROM dataset_versions WHERE id = %s", (dataset["id"],)
        ).fetchone()[0]
    snapshot = safe_path(h.root / "storage", key)
    before = fingerprint_tree(snapshot)
    universe = h.request(
        client,
        "POST",
        "/data/universes",
        status=201,
        json={
            "name": "Explicit user-fixed provider research basket",
            "state": "user_fixed",
            "as_of": datetime.now(UTC).isoformat(),
            "members": manifest["universe"]["members"],
            "evidence": [],
        },
    ).json()
    strategies = h.request(client, "GET", "/strategy-versions").json()["data"]
    seed = next(s for s in strategies if s["name"] == f"Original {task} seed")
    require(
        hashlib.sha256(
            (ROOT / "seed_strategies" / f"{task}.py").read_bytes()
        ).hexdigest()
        == seed["content_sha256"],
        "Original seed was substituted",
    )
    capabilities = h.request(client, "GET", "/research-capabilities").json()
    config = RunConfig.model_validate(
        {
            "execution_mode": "optimization",
            "task": task,
            "dataset": {"id": dataset["id"], "sha256": dataset["content_sha256"]},
            "seed_strategy": {"id": seed["id"], "sha256": seed["content_sha256"]},
            "universe": {"id": universe["id"], "sha256": universe["content_sha256"]},
            "windows": {
                "train": {"start": "2023-09-01", "end": "2023-10-16"},
                "validation": {"start": "2023-10-16", "end": "2023-12-01"},
                "holdout": {"start": "2023-12-01", "end": "2023-12-29"},
            },
            "warmup": {"sessions": 20},
            "market_profile": MarketConfig.for_market(task[:2].upper()).model_dump(
                mode="json"
            ),
            "optimizer": {"max_iterations": 1},
            "budgets": {
                "max_model_calls": 10,
                "max_tokens": 100000,
                "max_runtime_seconds": 180,
                "max_repairs": 0,
            },
            "environment_digest": capabilities["environment_digest"],
        }
    )
    preflight = h.request(
        client, "POST", "/research-runs/preflight", json=config.model_dump(mode="json")
    ).json()
    require(preflight["valid"], "Corrected same-snapshot preflight failed")
    run = h.request(
        client,
        "POST",
        "/research-runs",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
        json=config.model_dump(mode="json"),
    ).json()
    h.runs.append({"id": run["id"], "client": client, "config": config})
    done = h.until(
        lambda: h.finished(run["id"]),
        "Same-snapshot TradeTGD did not finish",
        timeout=200,
    )
    require(
        done["config"] == config.model_dump(mode="json")
        and done["dataset_version_id"] == dataset["id"],
        "Research input was substituted",
    )
    require(
        done["latest_checkpoint_id"] and done["result"]["best_candidate_id"],
        "Optimizer omitted persisted result/checkpoint",
    )
    candidates = research_rows(h, run["id"], "candidates")
    calls = research_rows(h, run["id"], "model-calls")
    evaluations = research_rows(h, run["id"], "evaluations")
    require(
        sorted(c["kind"] for c in candidates) == ["generated", "seed"],
        "One original TradeTGD iteration did not persist seed and candidate",
    )
    require(
        bool(calls) and all(c["provider"] == "fixture" for c in calls),
        "Frozen model receipts absent",
    )
    require(
        len(evaluations) == 4, "Seed/candidate train and validation evaluations missing"
    )
    for evaluation in evaluations:
        h.attempts.add(evaluation["id"])
        require(
            evaluation["sandbox_stopped"] and evaluation["status"] == "succeeded",
            "Docker evaluator termination unconfirmed",
        )
        require(
            evaluation["window"] in list(evaluation_windows(config)),
            "Holdout or another unplanned window executed",
        )
        evaluation_root = safe_path(
            h.root / "storage",
            f"runs/{run['id']}/attempts/{evaluation['attempt_id']}/evaluations/{evaluation['id']}",
        )
        report = json.loads((evaluation_root / "window-report.json").read_bytes())
        require(
            all(
                item["staged_warmup_sessions"] == 20
                and item["required_warmup_sessions"] == 20
                for item in report["listings"]
            ),
            "Actual Docker input omitted corrected warm-up history",
        )
        evaluation_request = json.loads(
            (evaluation_root / "input/request.json").read_bytes()
        )
        require(
            evaluation_request["market"] == task[:2].upper()
            and evaluation_request["profile"] == "corrected_explicit_config",
            "Evaluator market/profile drifted",
        )
        require(
            evaluation_request["evaluation_config"]["run_config_sha256"]
            == config.sha256,
            "Evaluator request did not pin same dataset/config",
        )
        record = h.request(client, "GET", f"/evaluations/{evaluation['id']}").json()
        ref = record["payload"]["result_bundle"]
        content = h.request(
            client, "GET", f"/artifacts/{ref['artifact_id']}/download"
        ).content
        require(
            hashlib.sha256(content).hexdigest() == ref["sha256"],
            "Result hash disagrees",
        )
        bundle = json.loads(content)
        validate_result_bundle(bundle)
        require(
            bundle["identity"]["run_config_sha256"] == config.sha256,
            "Persisted result config mismatch",
        )
        chart = h.request(
            client,
            "GET",
            f"/research-evaluations/{evaluation['id']}/results?sections=equity&sections=drawdown&max_points=600",
        ).json()
        require(bool(chart["tables"]["equity"]["rows"]), "Persisted chart lacks equity")
        require(
            h.request(
                client,
                "GET",
                f"/research-evaluations/{evaluation['id']}/results?sections=equity&sections=drawdown&max_points=600",
            ).json()
            == chart,
            "Persisted chart changed on read",
        )
        h.request(
            other,
            "GET",
            f"/research-evaluations/{evaluation['id']}/results",
            status=404,
        )
    require(
        all(a["sandbox_stopped"] for a in done["attempts"]),
        "Coordinator termination unconfirmed",
    )
    h.attempts.update(a["id"] for a in done["attempts"])
    exposure = h.request(
        client, "GET", f"/split-versions/{done['split_version_id']}/exposure"
    ).json()
    require(
        exposure["matching_count"] == 0
        and exposure["classification"] == "no_tracked_overlap",
        "Optimization exposed holdout",
    )
    require(
        fingerprint_tree(snapshot) == before,
        "Research or reads changed immutable provider bytes",
    )
    probe = h.request(
        client,
        "POST",
        f"/data/providers/{provider}/test",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "symbol": symbols[0],
            "start_date": "2024-01-02",
            "end_date": "2024-01-06",
        },
    ).json()
    receipt = wait_import(h, probe)
    require(
        receipt["dataset_version_id"] is None
        and receipt["progress"]["probe_receipt"]["outcome"] == "fixture_passed",
        "Connection test fabricated live verification or dataset",
    )
    h.request(other, "GET", f"/data/imports/{probe['id']}", status=404)
    if provider == "yfinance":
        repaired = wait_import(
            h,
            h.request(
                client,
                "POST",
                f"/datasets/{dataset['id']}/yahoo-repair",
                status=202,
                headers={"Idempotency-Key": uuid4().hex},
            ).json(),
        )
        derived = h.request(
            client, "GET", f"/datasets/{repaired['dataset_version_id']}"
        ).json()
        require(
            derived["manifest"]["derivation"]["manifest_sha256"]
            == dataset["content_sha256"],
            "Repair parent hash omitted",
        )
        require(
            derived["manifest"]["parent_version_id"] == dataset["id"],
            "Repair parent identity omitted",
        )
        require(
            all(
                p["effective_params"]["repair"]
                for p in derived["manifest"]["provenance"]
            ),
            "Repair=True not applied to derived acquisition",
        )
        require(
            fingerprint_tree(snapshot) == before,
            "Repair modified original provider snapshot",
        )


@pytest.mark.parametrize("provider_harness", ["deferred-data-worker"], indirect=True)
@pytest.mark.parametrize(
    "case",
    [
        "disabled_after_admission",
        "missing_revision",
        "wrong_owner",
        "wrong_provider",
        "missing_pin",
        "downgraded_pin",
    ],
)
def test_actual_worker_honors_frozen_connection_before_any_provider_or_checkpoint_access(
    provider_harness, case
):
    """No data workers exist until after disable/corruption, eliminating races."""
    from packages.marketdata.connections import make_snapshot

    h = provider_harness
    client = h.clients[0]
    admitted = h.request(
        client,
        "POST",
        "/data/imports",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "provider": "yfinance",
            "market": "US",
            "symbols": ["AAPL"],
            "start_date": "2024-01-02",
            "end_date": "2024-01-06",
        },
    ).json()
    binding = admitted["request"]["_provider_connection"]
    require(
        admitted["status"] == "requested",
        "Data worker started before the ordering barrier",
    )
    with h.connection(private=True) as connection:
        owner = connection.execute(
            "SELECT owner_id FROM data_imports WHERE id=%s", (admitted["id"],)
        ).fetchone()[0]
        require(
            connection.execute(
                "SELECT count(*) FROM data_blocks WHERE import_id=%s", (admitted["id"],)
            ).fetchone()[0]
            == 0,
            "Data access occurred before worker startup",
        )
        if case != "disabled_after_admission":
            changed = json.loads(json.dumps(admitted["request"]))
            if case == "missing_revision":
                changed["_provider_connection"] = make_snapshot(
                    owner, "yfinance", 999
                ).model_dump(mode="json")
            elif case == "wrong_owner":
                changed["_provider_connection"] = make_snapshot(
                    uuid4(), "yfinance", 1
                ).model_dump(mode="json")
            elif case == "wrong_provider":
                changed["_provider_connection"] = make_snapshot(
                    owner, "openbb", 1
                ).model_dump(mode="json")
            elif case == "missing_pin":
                changed.pop("_provider_connection")
            else:
                changed["_execution"]["worker_contract"] = "data-import.v2"
            # Test-only corruption in the harness-owned PostgreSQL schema. Never
            # mutate production settings/history or the immutable connection table.
            connection.execute(
                "UPDATE data_imports SET request=%s::jsonb WHERE id=%s",
                (json.dumps(changed), admitted["id"]),
            )
            connection.commit()
    h.request(
        client,
        "POST",
        "/data/provider-connections/yfinance",
        json={
            "expected_revision": binding["revision"],
            "enabled": False,
            "is_default": False,
            "default_price_mode": "provider_native",
        },
    )
    for role in ("dispatcher", "worker"):
        h.spawn(f"data-{role}", ["-m", "workers.data", role])

    def terminal():
        value = h.request(client, "GET", f"/data/imports/{admitted['id']}").json()
        return (
            value
            if value["status"] in {"ready", "partial", "failed", "cancelled"}
            else None
        )

    done = h.until(
        terminal, "Connection-bound data worker did not terminate", timeout=80
    )
    if case == "disabled_after_admission":
        require(
            done["status"] == "ready",
            "A later preference change incorrectly revoked the frozen job",
        )
        dataset = h.request(
            client, "GET", f"/datasets/{done['dataset_version_id']}"
        ).json()
        require(
            dataset["manifest"].get("provider_connection") == binding,
            "Published snapshot changed its accepted configuration",
        )
        require(done["request"] == admitted["request"], "Worker rewrote frozen request")
    else:
        require(
            done["status"] == "failed" and done["dataset_version_id"] is None,
            "Invalid persisted binding was executed",
        )
        require(
            not done["listing_ids"]
            and not done["progress"].get("resolution_provenance"),
            "Provider resolution happened before binding rejection",
        )
        require(
            done["blocks_total"] == 0,
            "Blocks/checkpoints were accessed before binding rejection",
        )
        with h.connection(private=True) as connection:
            require(
                connection.execute(
                    "SELECT count(*) FROM data_blocks WHERE import_id=%s",
                    (admitted["id"],),
                ).fetchone()[0]
                == 0,
                "Invalid binding created persisted data blocks",
            )
