"""PREPARED hosted synthetic HTTP + real SDK/PG/API/TradeTGD/sibling-container gate.

Not locally executed. Requires GITHUB_ACTIONS=true, model integration opt-in and
TRADEGRAD_REQUIRE_PORTABLE_MODEL_INTEGRATION=1, exact rebuilt portable images,
CREATEDB PostgreSQL and the three locked runtime interpreters. No live models,
market feeds, host candidate execution, SQLite or fake worker/runner fallback.
Collection does not construct servers, credentials, processes or databases.
"""

import os
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from synthetic_model_server import Reply, hosted_model_gate_enabled
from test_operator_model_pipeline import (
    OperatorModelHarness,
    assert_private,
    model_receipts,
    run_synthetic_profile_browser,
)
from test_operator_model_pipeline import (
    synthetic_server as synthetic_server,  # noqa: PLC0414
)
from test_research_optimization import runtime_paths
from test_worker_recovery import require

from packages.tradegrad_adapter.portable_config import PortableRunConfig
from packages.tradegrad_adapter.portable_environment import portable_environment_digest
from runtimes.evaluator.portable_runner import PortableDockerEvaluator

pytestmark = pytest.mark.skipif(
    not (
        hosted_model_gate_enabled()
        and os.getenv("TRADEGRAD_REQUIRE_PORTABLE_MODEL_INTEGRATION") == "1"
    ),
    reason="Portable operator gate requires explicit hosted-only synthetic HTTP opt-ins",
)


class PortableOperatorHarness(OperatorModelHarness):
    def __init__(self, root, database_url, interpreters, server):
        super().__init__(root, database_url, interpreters, server)
        self.deadline = time.monotonic() + 420
        self.runner = PortableDockerEvaluator(state_dir=root / "runner")
        self.data_started = False

    def remaining(self, maximum):
        value = min(maximum, self.deadline - time.monotonic())
        require(value > 0, "Portable operator gate exceeded its 420-second bound")
        return value

    def inputs(self, kind="event", market="US"):
        if not self.data_started:
            for role in ("dispatcher", "worker"):
                self.spawn(f"data-{role}", ["-m", "workers.data", role])
            self.data_started = True
        client = self.clients[0]
        symbols = ["600000"] if market == "CN" else ["AAPL"]
        if kind == "portfolio":
            symbols += ["000001"] if market == "CN" else ["MSFT"]
        job = self.request(
            client,
            "POST",
            "/data/imports",
            status=202,
            headers={"Idempotency-Key": uuid4().hex},
            json={
                "provider": "portable_fixture",
                "name": "Synthetic portable operator gate",
                "market": market,
                "symbols": symbols,
                "start_date": "2024-01-02",
                "end_date": "2024-03-01",
                "price_mode": "raw",
            },
        ).json()

        def imported():
            row = self.request(client, "GET", f"/data/imports/{job['id']}").json()
            require(
                row["status"] not in {"failed", "partial", "cancelled"},
                "Synthetic data import failed",
            )
            return row if row["status"] == "ready" else None

        ready = self.until(
            imported, "Synthetic raw fixture did not publish", timeout=70
        )
        dataset = self.request(
            client, "GET", f"/datasets/{ready['dataset_version_id']}"
        ).json()
        require(dataset["is_synthetic"], "Synthetic data label disappeared")
        universe = self.request(
            client,
            "POST",
            "/data/universes",
            status=201,
            json={
                "name": "Fixed synthetic operator basket",
                "state": "user_fixed",
                "as_of": datetime.now(UTC).isoformat(),
                "members": [{"listing_id": value} for value in ready["listing_ids"]],
                "evidence": [],
            },
        ).json()
        dataset_ref = {"id": dataset["id"], "sha256": dataset["content_sha256"]}
        universe_ref = {"id": universe["id"], "sha256": universe["content_sha256"]}
        seed = self.request(
            client,
            "POST",
            "/portable-strategies",
            status=201,
            json={
                "name": "Synthetic operator reference policy",
                "strategy_kind": kind,
                "dataset": dataset_ref,
                "universe": universe_ref,
            },
        ).json()
        body = self.request(
            client,
            "POST",
            "/portable-runs/default-config",
            json={
                "dataset": dataset_ref,
                "universe": universe_ref,
                "seed_strategy": {"id": seed["id"], "sha256": seed["content_sha256"]},
                "execution_mode": "optimization",
                "windows": {
                    "train": {"start": "2024-01-10", "end": "2024-02-01"},
                    "validation": {"start": "2024-02-01", "end": "2024-02-15"},
                    "holdout": {"start": "2024-02-15", "end": "2024-03-01"},
                },
            },
        ).json()
        caps = self.request(client, "GET", "/research-capabilities").json()
        selected = next(
            row for row in caps["model_profiles"] if row["id"] == "hosted-synthetic"
        )
        require(
            selected["synthetic"] and "portable" in selected["strategy_modes"],
            "Profile lacks portable synthetic admission",
        )
        require(
            body["model"]["provider"] == "fixture", "Default selected network calls"
        )
        foreign = self.request(self.clients[1], "GET", "/research-capabilities").json()
        require(
            all(row["id"] != selected["id"] for row in foreign["model_profiles"]),
            "Foreign profile was discoverable",
        )
        assert_private(caps, self.server)
        body["model"] = selected["config"]
        body["optimizer"].update(max_iterations=1, large_revision_prob=1)
        body["budgets"].update(
            max_model_calls=4, max_tokens=100000, max_runtime_seconds=180, max_repairs=0
        )
        return body, seed["code"]

    def submit_portable(self, body):
        client = self.clients[0]
        config = PortableRunConfig.model_validate(body)
        preflight = self.request(
            client, "POST", "/portable-runs/preflight", json=body
        ).json()
        require(
            preflight["config_sha256"] == config.sha256,
            "Preflight changed operator configuration",
        )
        key = uuid4().hex
        run = self.request(
            client,
            "POST",
            "/portable-runs",
            status=202,
            headers={"Idempotency-Key": key},
            json=body,
        ).json()
        repeat = self.request(
            client,
            "POST",
            "/portable-runs",
            status=202,
            headers={"Idempotency-Key": key},
            json=body,
        ).json()
        require(run["id"] == repeat["id"], "Repeated submission launched another run")
        self.runs.append({"id": run["id"], "client": client, "config": config})
        return run["id"]

    def terminal(self, run_id):
        row = self.request(self.clients[0], "GET", f"/portable-runs/{run_id}").json()
        return (
            row
            if row["status"]
            in {"succeeded", "failed", "cancelled", "interrupted", "paused"}
            else None
        )

    def rows(self, run_id, suffix):
        return self.request(
            self.clients[0], "GET", f"/portable-runs/{run_id}/{suffix}"
        ).json()["data"]

    def inspect_portable_privacy(self, run_id):
        for suffix in (
            "",
            "/model-calls",
            "/checkpoints",
            "/evaluations",
            "/candidates",
        ):
            response = self.request(
                self.clients[0], "GET", f"/portable-runs/{run_id}{suffix}"
            )
            assert_private(response.content, self.server)
            self.request(
                self.clients[1], "GET", f"/portable-runs/{run_id}{suffix}", status=404
            )
        for path in (self.root / "storage" / "runs" / run_id).rglob("*.json"):
            require(path.stat().st_size <= 50_000_000, "Oversized private evidence")
            assert_private(path.read_bytes(), self.server)


@pytest.fixture
def portable_operator_harness(tmp_path, synthetic_server):
    require(
        hosted_model_gate_enabled()
        and os.getenv("TRADEGRAD_REQUIRE_PORTABLE_MODEL_INTEGRATION") == "1",
        "Hosted portable operator opt-ins required",
    )
    url = os.getenv("TEST_DATABASE_URL")
    require(bool(url), "CREATEDB PostgreSQL URL is required")
    harness = None
    try:
        harness = PortableOperatorHarness(
            tmp_path, url, runtime_paths(), synthetic_server
        )
        harness.runner.bind_environment(portable_environment_digest())
        harness.setup()
        yield harness
    except Exception as error:  # noqa: BLE001 - no credentials or wire diagnostics
        pytest.fail(
            f"Portable operator hosted gate failed ({type(error).__name__})",
            pytrace=False,
        )
    finally:
        if harness is not None:
            harness.cleanup()


@pytest.mark.parametrize("kind,market", [("event", "CN"), ("portfolio", "US")])
def test_real_portable_trade_tgd_sdk_receipts_and_paused_checkpoint_replay(
    portable_operator_harness, kind, market
):
    h = portable_operator_harness
    body, code = h.inputs(kind, market)
    h.server.set_replies(
        [
            Reply(text="Synthetic bounded gradient"),
            Reply(text=f"```python\n{code}\n```", delay_seconds=3),
        ]
    )
    run_id = h.submit_portable(body)
    h.until(
        lambda: h.server.request_count == 2,
        "TradeTGD did not reach second bounded SDK call",
        timeout=110,
    )
    h.request(h.clients[0], "POST", f"/portable-runs/{run_id}/pause", status=202)
    paused = h.until(
        lambda: h.terminal(run_id),
        "Pause did not publish checkpoint and confirmed exits",
        timeout=110,
    )
    require(
        paused["status"] == "paused" and paused["latest_checkpoint_id"],
        "Pause did not preserve optimizer state",
    )
    calls = h.rows(run_id, "model-calls")
    require(
        len(calls) == 2 and all(row["status"] == "completed" for row in calls),
        "Completed provider calls were not durable",
    )
    require(
        all(row["usage"]["charged_tokens"] > 0 for row in calls),
        "Usage became unaccounted",
    )
    fingerprints = [
        (row["logical_call_id"], row["request_sha256"], row["response_sha256"])
        for row in calls
    ]
    receipts = model_receipts(h, run_id)
    require(
        [kind for kind, _ in receipts]
        == ["model_process_admitted", "model_process_stopped"] * 2,
        "SDK local process receipts are incomplete",
    )
    h.request(h.clients[0], "POST", f"/portable-runs/{run_id}/resume", status=202)
    final = h.until(
        lambda: h.terminal(run_id), "Checkpoint resume did not terminate", timeout=140
    )
    require(
        final["status"] == "succeeded" and h.server.request_count == 2,
        "Resume retried model requests or failed",
    )
    require(
        fingerprints
        == [
            (row["logical_call_id"], row["request_sha256"], row["response_sha256"])
            for row in h.rows(run_id, "model-calls")
        ],
        "Resume changed logical request/response identity",
    )
    require(
        all(row["sandbox_stopped"] for row in final["attempts"]),
        "Final sibling/model stop was not confirmed",
    )
    evaluations = h.rows(run_id, "evaluations")
    require(
        all(
            row["split"] in {"train", "validation"} and row["sandbox_stopped"]
            for row in evaluations
        ),
        "Evaluation escaped allowed window or containment",
    )
    for evaluation in evaluations:
        h.attempts.add(evaluation["id"])
        result = h.request(
            h.clients[0], "GET", f"/portable-evaluations/{evaluation['id']}/results"
        ).json()
        require(
            result["ledger_origin"] == "trusted_engine"
            and result["runtime_certified"] is False,
            "Result trust boundary changed",
        )
    h.inspect_portable_privacy(run_id)


@pytest.mark.skipif(
    os.getenv("TRADEGRAD_REQUIRE_MODEL_BROWSER") != "1",
    reason="Portable profile Chromium requires the additional hosted browser opt-in",
)
def test_real_portable_chromium_profile_selection_without_model_calls(
    portable_operator_harness,
):
    # A separate ninth B9 gate preserves the historical 18-case native target.
    run_synthetic_profile_browser(portable_operator_harness, mode="portable")


@pytest.mark.parametrize(
    "kind,delay,expected",
    [
        ("malformed_json", 0, "failed"),
        ("server_error", 0, "failed"),
        ("success", 10, "started"),
    ],
)
def test_real_portable_sdk_failures_never_retry_or_erase_usage(
    portable_operator_harness, kind, delay, expected
):
    h = portable_operator_harness
    body, _ = h.inputs()
    h.server.set_replies([Reply(kind, "Synthetic failure response", delay)])
    run_id = h.submit_portable(body)
    final = h.until(
        lambda: h.terminal(run_id),
        "Failed model request did not terminate",
        timeout=110,
    )
    calls = h.rows(run_id, "model-calls")
    require(
        final["status"] == "failed" and h.server.request_count == 1 and len(calls) == 1,
        "Failure retried or became success",
    )
    require(
        calls[0]["status"] == expected and calls[0]["usage"]["charged_tokens"] > 0,
        "Failure lost uncertainty/accounting",
    )
    require(
        all(row["sandbox_stopped"] for row in final["attempts"]),
        "Failure left unconfirmed execution",
    )
    h.inspect_portable_privacy(run_id)


def test_real_portable_cancellation_waits_for_model_stop(portable_operator_harness):
    h = portable_operator_harness
    body, _ = h.inputs()
    h.server.set_replies(
        [Reply(text="Synthetic cancellation response", delay_seconds=3)]
    )
    run_id = h.submit_portable(body)
    h.until(
        lambda: h.server.request_count == 1,
        "No model request reached synthetic server",
        timeout=110,
    )
    h.request(h.clients[0], "POST", f"/portable-runs/{run_id}/cancel", status=202)
    final = h.until(
        lambda: h.terminal(run_id),
        "Cancellation exceeded bounded transport cleanup",
        timeout=15,
    )
    require(
        final["status"] == "cancelled" and h.server.request_count == 1,
        "Cancellation launched another request",
    )
    require(
        [kind for kind, _ in model_receipts(h, run_id)]
        == ["model_process_admitted", "model_process_stopped"],
        "Cancelled request lacks durable stop",
    )
    h.inspect_portable_privacy(run_id)


@pytest.mark.parametrize("budget", ["max_model_calls", "max_tokens"])
def test_real_portable_budget_exhaustion_prevents_first_http(
    portable_operator_harness, budget
):
    h = portable_operator_harness
    body, _ = h.inputs()
    body["budgets"][budget] = 0
    h.server.set_replies([])
    run_id = h.submit_portable(body)
    final = h.until(
        lambda: h.terminal(run_id), "Budget-exhausted run did not stop", timeout=110
    )
    require(
        final["status"] == "succeeded" and h.server.request_count == 0,
        "Budget limit launched model HTTP",
    )
    require(
        not h.rows(run_id, "model-calls") and not model_receipts(h, run_id),
        "Budget denial fabricated calls",
    )
    h.inspect_portable_privacy(run_id)
