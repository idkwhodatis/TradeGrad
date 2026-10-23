"""HOSTED opt-in synthetic HTTP -> real SDK -> production TradeTGD gate.

Requires GITHUB_ACTIONS=true AND TRADEGRAD_REQUIRE_MODEL_INTEGRATION=1.
Application cases additionally require CREATEDB PostgreSQL, the prebuilt reviewed
Docker evaluator, and distinct API_PYTHON/RESEARCH_PYTHON/DATA_PYTHON runtimes.
No real endpoint, account, paid model, market feed, or host-executed candidate is
allowed. Keys are generated per fixture and sent only to its loopback fixture.
Collection and opt-out do not start sockets, subprocesses, databases or Docker.
These are prepared hosted gates, not claims that local/live acceptance passed.
"""

from __future__ import annotations

import json
import os
import secrets
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from operator_browser_launcher import browser_command, browser_environment
from synthetic_model_server import (
    Reply,
    SyntheticModelServer,
    hosted_model_gate_enabled,
)
from test_research_optimization import (
    ResearchProcessHarness,
    kill_owned_worker_at_generated_evaluation,
    logical_lineage,
    model_fingerprints,
    research_rows,
    runtime_paths,
)
from test_worker_recovery import ROOT, require

from backend.app.storage import safe_path
from packages.tradegrad_adapter.checkpoint import (
    CheckpointRef,
    CheckpointStore,
    json_digest,
)
from packages.tradegrad_adapter.config import (
    MarketConfig,
    RunConfig,
    evaluation_windows,
)
from packages.tradegrad_adapter.model_calls import OptimizerStop, RecordedModel
from script.ci_browser_diagnostics import failure_diagnostic, private_browser_report
from script.ci_owned_group import (
    admit_owned_group,
    peek_owned_group,
    signal_owned_group,
    signal_owned_process,
    wait_owned_group,
)

pytestmark = pytest.mark.skipif(
    not hosted_model_gate_enabled(),
    reason="Synthetic model HTTP is allowed only in explicitly opted-in hosted CI",
)


def operator_profile(server, owner_id, *, timeout=4):
    return {
        "id": "hosted-synthetic",
        "label": "Synthetic HTTP fixture, NOT live model evidence",
        "provider": "openai-compatible",
        "model": "synthetic-tradegrad-v1",
        "base_url": server.base_url,
        "secret_env": server.secret_env,
        "owner_ids": [str(owner_id)],
        "temperature": 0,
        "max_output_tokens": 4096,
        "request_timeout_seconds": timeout,
        "max_request_bytes": 1024 * 1024,
        "max_response_bytes": 1024 * 1024,
        "synthetic": True,
    }


def assert_private(value, server):
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    for private in (server.secret, server.base_url, server.secret_env):
        require(
            private.encode() not in raw, "Model operator credentials or route leaked"
        )


def sdk_provider(server, monkeypatch, *, timeout=2):
    # Use the production resolver/factory, not a test-only OpenAI client or model.
    from backend.app.core.config import Settings
    from workers.research.model_provider import create_model

    owner_id = uuid4()
    settings = Settings(
        database_url="postgresql+psycopg://unused/unused",
        secret_key=secrets.token_urlsafe(48),
        data_provider_mode="fixture",
        model_profiles_enabled=True,
        model_synthetic_test_mode=True,
        model_profiles=[operator_profile(server, owner_id, timeout=timeout)],
    )
    monkeypatch.setenv(server.secret_env, server.secret)
    profile = settings.model_profiles[0]
    config = SimpleNamespace(model=profile.public_config())
    return create_model(config, settings, owner_id, "# Never executed by SDK tests\n")


def recorded_model(root, provider):
    model = RecordedModel(
        root,
        input_digest="a" * 64,
        provider=provider,
        budgets={"max_model_calls": 2, "max_tokens": 10000, "max_runtime_seconds": 10},
        parameters={"temperature": 0, "max_output_tokens": 100},
    )
    model.set_context("synthetic-sdk")
    return model


@pytest.fixture
def synthetic_server():
    # Check again at execution time; importing this module can never authorize it.
    require(
        hosted_model_gate_enabled(), "Both hosted model integration gates are required"
    )
    with SyntheticModelServer() as server:
        yield server


@pytest.mark.parametrize("usage", ["success", "unknown_usage"])
def test_real_sdk_success_replays_locally_without_second_http(
    synthetic_server, monkeypatch, tmp_path, usage
):
    server = synthetic_server
    server.set_replies([Reply(usage, "bounded synthetic completion")])
    provider = sdk_provider(server, monkeypatch)
    first = recorded_model(tmp_path / "journal", provider)
    require(
        first.query("synthetic prompt") == "bounded synthetic completion",
        "SDK response differs",
    )
    require(
        server.request_count == 1, "SDK did not make exactly one local HTTP request"
    )
    replay = recorded_model(tmp_path / "journal", provider)
    require(
        replay.query("synthetic prompt") == "bounded synthetic completion",
        "Durable replay differs",
    )
    require(server.request_count == 1, "Completed replay issued another HTTP request")
    row = replay.records[0]
    require(
        row["status"] == "completed" and row["charged_tokens"] > 0,
        "Usage was not accounted",
    )
    if usage == "unknown_usage":
        require(
            row["input_tokens"] is None
            and row["usage_source"] == "conservative_reservation",
            "Unknown usage was fabricated",
        )
    else:
        require(
            row["input_tokens"] == 20 and row["output_tokens"] == 30,
            "SDK usage did not survive recording",
        )
    assert_private(replay.records, server)
    require(provider.close(), "SDK request left an uncontained child")
    require(
        all(row["authorized"] for row in server.audit),
        "Synthetic key did not reach its own endpoint",
    )


@pytest.mark.parametrize(
    "kind,delay,expected",
    [
        ("rate_limit", 0, "failed"),
        ("server_error", 0, "failed"),
        ("malformed_json", 0, "failed"),
        ("missing_choices", 0, "failed"),
        ("invalid_usage", 0, "failed"),
        ("oversized", 0, "failed"),
        ("redirect", 0, "failed"),
        ("success", 5, "started"),
    ],
)
def test_real_sdk_failures_are_bounded_conservative_and_never_retried(
    synthetic_server, monkeypatch, tmp_path, kind, delay, expected
):
    server = synthetic_server
    server.set_replies([Reply(kind, "Synthetic error " + server.secret, delay)])
    provider = sdk_provider(server, monkeypatch, timeout=2)
    first = recorded_model(tmp_path / "journal", provider)
    started = time.monotonic()
    with pytest.raises(OptimizerStop):
        first.query("synthetic failure prompt")
    require(time.monotonic() - started < 8, "SDK request escaped its hard time bound")
    require(server.request_count == 1, "SDK failure retried the request")
    require(
        first.records[0]["status"] == expected,
        "Failure lost known/unknown outcome classification",
    )
    require(
        first.records[0]["charged_tokens"] > 0,
        "Failed or unknown usage was silently free",
    )
    replay = recorded_model(tmp_path / "journal", provider)
    with pytest.raises(OptimizerStop):
        replay.query("synthetic failure prompt")
    require(
        server.request_count == 1,
        "Failed or uncertain call was sent again during replay",
    )
    assert_private(replay.records, server)
    for path in (tmp_path / "journal").glob("*.json"):
        assert_private(path.read_bytes(), server)
    require(provider.close(), "Failed SDK request left an uncontained child")


class OperatorModelHarness(ResearchProcessHarness):
    def __init__(self, root, database_url, interpreters, server, profile_timeout=4):
        super().__init__(root, database_url, interpreters)
        self.server = server
        self.profile_timeout = profile_timeout
        self.account_emails = []
        self.browser_accounts = []
        # Disallow an inherited deployment registry or key from changing this gate.
        self.env.update(
            MODEL_PROFILES_ENABLED="true",
            MODEL_SYNTHETIC_TEST_MODE="true",
            MODEL_PROFILES="[]",
        )
        self.env.pop(server.secret_env, None)

    def spawn(self, label, arguments, *, persistent=True, extra_env=None):
        if label == "bootstrap":
            email = arguments[arguments.index("--email") + 1]
            self.account_emails.append(email)
            self.browser_accounts.append(
                (email, (extra_env or {})["PROCESS_TEST_PASSWORD"])
            )
        if label == "api":
            with self.connection(private=True) as connection:
                owner = connection.execute(
                    "SELECT id FROM users WHERE email=%s", (self.account_emails[0],)
                ).fetchone()
            require(owner is not None, "Synthetic owner bootstrap failed")
            self.env["MODEL_PROFILES"] = json.dumps(
                [operator_profile(self.server, owner[0], timeout=self.profile_timeout)]
            )
        extra = dict(extra_env or {})
        if arguments[:3] == ["-m", "workers.research", "worker"]:
            extra[self.server.secret_env] = self.server.secret
        else:
            require(
                self.server.secret_env not in self.env
                and self.server.secret_env not in extra,
                "Synthetic key escaped the research worker",
            )
        return super().spawn(label, arguments, persistent=persistent, extra_env=extra)

    def submit_operator_run(self, dataset, universe):
        client = self.clients[0]
        capability = self.request(client, "GET", "/research-capabilities").json()
        profiles = capability["model_profiles"]
        selected = next(row for row in profiles if row["id"] == "hosted-synthetic")
        require(selected["synthetic"], "Synthetic operator profile lost its warning")
        require(
            profiles[0]["config"]["provider"] == "fixture",
            "Fixture ceased to be the default",
        )
        assert_private(capability, self.server)
        other_capability = self.request(
            self.clients[1], "GET", "/research-capabilities"
        ).json()
        require(
            all(
                row["id"] != selected["id"]
                for row in other_capability["model_profiles"]
            ),
            "Another owner discovered a private model profile",
        )
        strategy = next(
            row
            for row in self.request(client, "GET", "/strategy-versions").json()["data"]
            if row["name"] == "Original cn_time seed"
        )
        config = RunConfig.model_validate(
            {
                "execution_mode": "optimization",
                "task": "cn_time",
                "dataset": {"id": dataset["id"], "sha256": dataset["content_sha256"]},
                "seed_strategy": {
                    "id": strategy["id"],
                    "sha256": strategy["content_sha256"],
                },
                "universe": {
                    "id": universe["id"],
                    "sha256": universe["content_sha256"],
                },
                "windows": {
                    "train": {"start": "2024-02-01", "end": "2024-03-15"},
                    "validation": {"start": "2024-03-15", "end": "2024-04-20"},
                    "holdout": {"start": "2024-04-22", "end": "2024-04-27"},
                },
                "warmup": {"sessions": 20},
                "market_profile": MarketConfig.for_market("CN").model_dump(mode="json"),
                "optimizer": {"max_iterations": 1, "large_revision_prob": 1},
                "budgets": {
                    "max_model_calls": 4,
                    "max_tokens": 100000,
                    "max_runtime_seconds": 150,
                    "max_repairs": 0,
                },
                "model": selected["config"],
                "environment_digest": capability["environment_digest"],
            }
        )
        run = self.request(
            client,
            "POST",
            "/research-runs",
            status=202,
            headers={"Idempotency-Key": uuid4().hex},
            json=config.model_dump(mode="json"),
        ).json()
        self.runs.append({"id": run["id"], "client": client, "config": config})
        return run["id"], config

    def terminal(self, run_id):
        detail = self.request(self.clients[0], "GET", f"/research-runs/{run_id}").json()
        return (
            detail
            if detail["status"] in {"succeeded", "failed", "cancelled", "interrupted"}
            else None
        )

    def inspect_privacy(self, run_id):
        client, other = self.clients
        for suffix in (
            "",
            "/candidates",
            "/model-calls",
            "/checkpoints",
            "/evaluations",
        ):
            response = self.request(client, "GET", f"/research-runs/{run_id}{suffix}")
            assert_private(response.content, self.server)
            self.request(other, "GET", f"/research-runs/{run_id}{suffix}", status=404)
        stream = self.request(client, "GET", f"/runs/{run_id}/events?follow=false")
        assert_private(stream.content, self.server)
        root = self.root / "storage" / "runs" / run_id
        for path in root.rglob("*.json"):
            require(
                path.stat().st_size <= 50_000_000,
                "Unexpectedly large application evidence file",
            )
            assert_private(path.read_bytes(), self.server)


@pytest.fixture
def model_harness(tmp_path, synthetic_server, request):
    require(
        hosted_model_gate_enabled(), "Both hosted model integration gates are required"
    )
    url = os.getenv("TEST_DATABASE_URL")
    require(
        bool(url),
        "TEST_DATABASE_URL is required for hosted model application acceptance",
    )
    harness = None
    try:
        harness = OperatorModelHarness(
            tmp_path,
            url,
            runtime_paths(),
            synthetic_server,
            profile_timeout=getattr(request, "param", 4),
        )
        harness.setup()
        yield harness
    except Exception as error:  # noqa: BLE001 - never disclose settings, credentials or HTTP payloads
        pytest.fail(
            f"Hosted synthetic model application failed ({type(error).__name__})",
            pytrace=False,
        )
    finally:
        if harness is not None:
            harness.cleanup()


def test_real_worker_sdk_generation_sigkill_checkpoint_replay_and_privacy(
    model_harness,
):
    h = model_harness
    # This source is only evaluated in the actual production Docker sandbox.
    code = (
        "import time as _synthetic_time\n_synthetic_time.sleep(8)\n"
        + (ROOT / "seed_strategies/cn_time.py").read_text()
    )
    h.server.set_replies(
        [
            Reply(
                text="Synthetic gradient. Authorization: Bearer "
                + h.server.secret
                + " endpoint "
                + h.server.base_url
            ),
            Reply(text="```python\n" + code + "\n```"),
        ]
    )
    dataset, universe = h.upload_fixture()
    run_id, config = h.submit_operator_run(dataset, universe)
    old = h.until(
        lambda: kill_owned_worker_at_generated_evaluation(h, run_id),
        "Generated Docker evaluation never reached the real-worker replay boundary",
        timeout=100,
    )
    require(
        h.server.request_count == 2,
        "TradeTGD did not issue gradient and generation through the SDK",
    )
    receipts = model_receipts(h, run_id)
    require(
        [kind for kind, _ in receipts]
        == ["model_process_admitted", "model_process_stopped"] * 2,
        "Completed SDK calls lack durable containment receipts before crash replay",
    )
    h.until(lambda: h.terminal(run_id), "Killed worker was not reconciled", timeout=30)
    detail = h.terminal(run_id)
    require(
        detail["status"] == "interrupted"
        and all(row["sandbox_stopped"] for row in detail["attempts"]),
        "Killed attempt was not durably contained",
    )
    resumed = h.request(
        h.clients[0], "POST", f"/research-runs/{run_id}/resume", status=202
    ).json()
    require(
        resumed["resume_checkpoint_id"] == old["checkpoint_id"],
        "Resume selected an unverified checkpoint",
    )
    h.workers = h.start_workers()
    final = h.until(
        lambda: h.finished(run_id),
        "Real SDK-backed resumed optimization did not finish",
        timeout=100,
    )
    require(
        h.server.request_count == 2,
        "Checkpoint resume repeated a completed HTTP request",
    )
    require(
        model_fingerprints(research_rows(h, run_id, "model-calls")) == old["calls"],
        "Model-call identity or accounting changed across replay",
    )
    require(
        logical_lineage(research_rows(h, run_id, "candidates")) == old["lineage"],
        "Generated candidate lineage changed across replay",
    )
    calls = research_rows(h, run_id, "model-calls")
    require(
        len(calls) == 2
        and all(row["provider"] == "openai-compatible" for row in calls),
        "Frozen fixture was substituted for the real SDK path",
    )
    evaluations = research_rows(h, run_id, "evaluations")
    h.attempts.update(row["id"] for row in evaluations)
    require(
        all(
            row["split"] != "holdout"
            and row["window"] in list(evaluation_windows(config))
            for row in evaluations
        ),
        "Research escaped its planned non-holdout windows",
    )
    require(
        all(row["sandbox_stopped"] for row in final["attempts"]),
        "Resumed attempt termination is unconfirmed",
    )
    with h.connection(private=True) as connection:
        pointer = connection.execute(
            "SELECT manifest_storage_key, manifest_sha256, input_digest FROM research_checkpoints WHERE id=%s AND complete=true",
            (final["latest_checkpoint_id"],),
        ).fetchone()
    require(pointer is not None, "Real worker did not publish a complete checkpoint")
    state = CheckpointStore.load(
        CheckpointRef(
            str(safe_path(h.root / "storage", pointer[0])), pointer[1], pointer[2]
        ),
        expected_input_digest=pointer[2],
    )
    require(
        state["next_stage"] == "finished"
        and state["inputs"]["config"] == config.model_dump(mode="json"),
        "Checkpoint did not preserve the pinned operator profile",
    )
    assert_private(state, h.server)
    h.inspect_privacy(run_id)


@pytest.mark.parametrize(
    "kind,delay,expected",
    [
        ("malformed_json", 0, "failed"),
        ("server_error", 0, "failed"),
        ("success", 10, "started"),
    ],
)
def test_real_worker_http_failure_preserves_usage_and_no_retries(
    model_harness, kind, delay, expected
):
    h = model_harness
    h.server.set_replies([Reply(kind, "Synthetic error " + h.server.secret, delay)])
    dataset, universe = h.upload_fixture()
    run_id, _ = h.submit_operator_run(dataset, universe)
    final = h.until(
        lambda: h.terminal(run_id),
        "Failed model request did not stop the production worker",
        timeout=100,
    )
    require(
        final["status"] == "failed", "Invalid model response produced a successful run"
    )
    calls = research_rows(h, run_id, "model-calls")
    require(
        h.server.request_count == 1 and len(calls) == 1,
        "Worker or source retry loop repeated a model request",
    )
    require(
        calls[0]["status"] == expected, "Worker lost uncertain model outcome evidence"
    )
    require(
        calls[0]["usage"]["charged_tokens"] > 0,
        "Failure lost conservative usage accounting",
    )
    require(
        all(row["sandbox_stopped"] for row in final["attempts"]),
        "Failed worker has unconfirmed containment",
    )
    h.inspect_privacy(run_id)


def test_real_worker_cancellation_during_http_is_bounded(model_harness):
    h = model_harness
    h.server.set_replies(
        [Reply(text="Synthetic bounded cancellation response", delay_seconds=3)]
    )
    dataset, universe = h.upload_fixture()
    run_id, _ = h.submit_operator_run(dataset, universe)
    h.until(
        lambda: h.server.request_count == 1,
        "Worker did not reach local HTTP boundary",
        timeout=100,
    )
    start = time.monotonic()
    h.request(h.clients[0], "POST", f"/research-runs/{run_id}/cancel", status=202)
    final = h.until(
        lambda: h.terminal(run_id),
        "Cancellation escaped the configured HTTP/cleanup bound",
        timeout=15,
    )
    require(
        time.monotonic() - start < 15 and final["status"] == "cancelled",
        "Cancellation did not finish within the transport bound",
    )
    require(h.server.request_count == 1, "Cancellation generated another model request")
    require(
        all(row["sandbox_stopped"] for row in final["attempts"]),
        "Cancelled worker containment is unconfirmed",
    )
    h.inspect_privacy(run_id)


def process_identity(pid):
    """Read only an already-owned process identity; never scan system processes."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0], fields[
            19
        ]  # state and start time, protected against PID reuse
    except FileNotFoundError:
        return None


def stop_owned_sdk_child(pid, identity):
    """Signal only a pidfd pinned to the previously verified SDK process.

    Unlike the unreaped Popen-owned worker, this reparented child could already have
    been reaped. Never use its numeric PID/process group as a signal destination.
    """
    open_pidfd = getattr(os, "pidfd_open", None)
    send_signal = getattr(signal, "pidfd_send_signal", None)
    if open_pidfd is None or send_signal is None:
        return False
    try:
        descriptor = open_pidfd(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    try:
        # Recheck after opening: if PID reuse preceded pidfd_open, that descriptor
        # belongs to somebody else and must not receive even a harmless signal.
        current = process_identity(pid)
        if current is None or current[1] != identity[1] or current[0] == "Z":
            return True
        try:
            send_signal(descriptor, signal.SIGKILL, None, 0)
        except ProcessLookupError:
            return True
        poller = select.poll()
        poller.register(descriptor, select.POLLIN)
        return any(
            fd == descriptor and events & (select.POLLIN | select.POLLHUP)
            for fd, events in poller.poll(2000)
        )
    except OSError:
        return False
    finally:
        os.close(descriptor)


def owned_sdk_child(h):
    require(len(h.workers) == 1, "SDK containment proof requires one owned worker")
    worker = h.workers[0]
    require(
        peek_owned_group(worker) is None
        and os.getpgid(worker.pid) == worker.pid != os.getpgrp(),
        "Worker process group is not exclusively owned",
    )
    require(
        any(process is worker for _, process in h.processes),
        "Worker PID was not created by this harness",
    )
    # Popen may be called by a worker thread. Inspect only tasks of this exact
    # recorded worker, and require the exact reviewed SDK child command.
    sdk_command = str(ROOT / "packages/tradegrad_adapter/model_sdk_process.py").encode()
    children = set()
    for task in Path(f"/proc/{worker.pid}/task").iterdir():
        try:
            children.update(
                int(value) for value in (task / "children").read_text().split()
            )
        except FileNotFoundError:
            continue
    sdk_children = []
    for pid in children:
        try:
            if sdk_command in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0"):
                sdk_children.append(pid)
        except FileNotFoundError:
            continue
    require(
        len(sdk_children) == 1,
        "Could not identify the actual owned in-flight SDK child",
    )
    sdk_pid = sdk_children[0]
    identity = process_identity(sdk_pid)
    require(
        identity is not None and identity[0] != "Z",
        "SDK child already stopped before fault injection",
    )
    require(
        os.getpgid(sdk_pid) == sdk_pid,
        "SDK child does not have an isolated owned process group",
    )
    return worker, sdk_pid, identity


def blocked_recovery(h, run_id):
    stream = h.request(h.clients[0], "GET", f"/runs/{run_id}/events?follow=false").text
    events = [
        json.loads(line[6:])
        for line in stream.splitlines()
        if line.startswith("data: ")
    ]
    if not any(event["type"] == "recovery_blocked" for event in events):
        return None
    return h.request(h.clients[0], "GET", f"/research-runs/{run_id}").json()


def model_receipts(h, run_id):
    with h.connection(private=True) as connection:
        return connection.execute(
            "SELECT type, payload FROM run_events WHERE run_id=%s "
            "AND type IN ('model_process_admitted', 'model_process_stopped') ORDER BY seq",
            (run_id,),
        ).fetchall()


@pytest.mark.parametrize("model_harness", [12], indirect=True)
def test_real_sdk_child_dies_on_worker_sigkill_but_missing_receipt_blocks_recovery(
    model_harness,
):
    h = model_harness
    h.server.set_replies(
        [Reply(text="Synthetic in-flight unknown response", delay_seconds=10)]
    )
    dataset, universe = h.upload_fixture()
    run_id, _ = h.submit_operator_run(dataset, universe)
    h.until(
        lambda: h.server.request_count == 1,
        "Worker never issued the in-flight SDK request",
        timeout=100,
    )
    worker, sdk_pid, identity = owned_sdk_child(h)
    h.expected_alive.discard(worker)
    signal_owned_group(worker, signal.SIGKILL)
    require(
        wait_owned_group(worker, timeout=h.remaining(5)) == -signal.SIGKILL,
        "Owned worker did not stop on SIGKILL",
    )
    h.processes = [
        (label, process) for label, process in h.processes if process is not worker
    ]

    def child_stopped():
        current = process_identity(sdk_pid)
        # A dead orphan may briefly await init's reap; it cannot execute/network.
        return current is None or current[1] != identity[1] or current[0] == "Z"

    try:
        h.until(
            child_stopped, "SDK child survived its parent death boundary", timeout=2
        )
    finally:
        # Exact pidfd cleanup after a failed proof does not make the proof pass.
        # No numeric PID/group fallback is safe for a reparented/reaped child.
        if not child_stopped():
            require(
                stop_owned_sdk_child(sdk_pid, identity),
                "SDK child cleanup could not confirm termination with an exact pidfd",
            )
    blocked = h.until(
        lambda: blocked_recovery(h, run_id),
        "Reconciler did not block the unmatched SDK admission",
        timeout=30,
    )
    require(
        blocked["status"] == "running"
        and blocked["generation"] == 1
        and len(blocked["attempts"]) == 1
        and not blocked["attempts"][0]["sandbox_stopped"]
        and blocked["attempts"][0]["finished_at"] is None,
        "Local observation was incorrectly treated as persisted SDK stop proof",
    )
    h.request(h.clients[0], "POST", f"/research-runs/{run_id}/resume", status=409)
    receipts = model_receipts(h, run_id)
    require(
        len(receipts) == 1 and receipts[0][0] == "model_process_admitted",
        "Killed worker fabricated a confirmed SDK stop receipt",
    )
    require(
        h.server.request_count == 1,
        "Blocked recovery charged or reissued the in-flight unknown request",
    )
    attempt_id = blocked["attempts"][0]["id"]
    journal = (
        h.root / "storage" / "runs" / run_id / "attempts" / attempt_id / "model_calls"
    )
    records = [json.loads(path.read_bytes()) for path in journal.glob("*.json")]
    require(len(records) == 1, "Uncertain call journal was lost")
    row = records[0]["record"]
    require(
        records[0]["sha256"] == json_digest(row)
        and row["status"] == "started"
        and row["charged_tokens"] > 0,
        "Blocked unknown call lost its conservative verified journal",
    )
    with h.connection(private=True) as connection:
        jobs = connection.execute(
            "SELECT count(*) FROM outbox_events WHERE run_id=%s", (run_id,)
        ).fetchone()[0]
    require(jobs == 1, "Blocked resume released another queue generation")
    h.inspect_privacy(run_id)


@pytest.mark.parametrize("model_harness", [30], indirect=True)
def test_stale_living_worker_blocks_recovery_until_confirmed_sdk_stop_receipt(
    model_harness,
):
    h = model_harness
    h.server.set_replies(
        [Reply(text="Synthetic stale-worker response", delay_seconds=15)]
    )
    dataset, universe = h.upload_fixture()
    run_id, _ = h.submit_operator_run(dataset, universe)
    h.until(
        lambda: h.server.request_count == 1,
        "Worker never reached stale SDK boundary",
        timeout=100,
    )
    worker, _sdk_pid, _sdk_identity = owned_sdk_child(h)
    worker_identity = process_identity(worker.pid)
    require(worker_identity is not None, "Owned worker exited before SIGSTOP")
    signal_owned_group(worker, signal.SIGSTOP)
    try:
        blocked = h.until(
            lambda: blocked_recovery(h, run_id),
            "Expired living worker did not block reconciliation",
            timeout=30,
        )
        require(
            blocked["status"] == "running"
            and blocked["generation"] == 1
            and not blocked["attempts"][0]["sandbox_stopped"]
            and blocked["attempts"][0]["finished_at"] is None,
            "Lease expiry incorrectly confirmed an unreceipted SDK process",
        )
        current = process_identity(worker.pid)
        require(
            current is not None and current[0] == "T",
            "Owned worker was not frozen at the expired-lease boundary",
        )
        receipts = model_receipts(h, run_id)
        require(
            len(receipts) == 1 and receipts[0][0] == "model_process_admitted",
            "Frozen worker fabricated a confirmed SDK stop receipt",
        )
        h.request(h.clients[0], "POST", f"/research-runs/{run_id}/resume", status=409)
        require(
            h.server.request_count == 1,
            "Blocked stale-worker recovery issued another HTTP request",
        )
    finally:
        current = process_identity(worker.pid)
        if (
            current is not None
            and current[1] == worker_identity[1]
            and peek_owned_group(worker) is None
        ):
            signal_owned_group(worker, signal.SIGCONT)
    final = h.until(
        lambda: h.terminal(run_id),
        "Confirmed SDK stop receipt did not unblock reconciliation",
        timeout=35,
    )
    require(
        final["status"] == "interrupted"
        and final["generation"] == 1
        and len(final["attempts"]) == 1
        and final["attempts"][0]["sandbox_stopped"],
        "Stale worker completion regained old lease authority or bypassed containment",
    )
    require(
        h.server.request_count == 1,
        "Stale worker issued another SDK call after lease expiry",
    )
    receipts = model_receipts(h, run_id)
    require(
        [kind for kind, _ in receipts]
        == ["model_process_admitted", "model_process_stopped"]
        and receipts[0][1] == receipts[1][1],
        "Unfenced cleanup did not publish the exact admitted call's stop receipt",
    )
    h.inspect_privacy(run_id)


@pytest.mark.skipif(
    os.getenv("TRADEGRAD_REQUIRE_MODEL_BROWSER") != "1",
    reason="Configured-profile Chromium launcher requires its separate hosted opt-in",
)
def test_real_chromium_synthetic_profile_seed_only_without_model_calls(model_harness):
    run_synthetic_profile_browser(model_harness)


def run_synthetic_profile_browser(h, *, mode="native_akquant"):
    """Same generated owner/API/server, secret filter and bounded subreaper in both modes."""
    with private_browser_report(h.root) as report:
        _run_synthetic_profile_browser(h, mode=mode, report=report)


def _run_synthetic_profile_browser(h, *, mode, report):
    # This one case has a separate finite browser/setup budget; the original 17
    # SDK/application cases retain their unchanged 240-second harness bounds.
    h.deadline = time.monotonic() + 390
    require(len(h.browser_accounts) == 2, "Generated browser accounts are unavailable")
    node = shutil.which("node", path=os.environ.get("PATH", ""))
    require(node is not None, "The installed Node runtime is required")
    browser_command(node, ROOT, mode)
    if mode == "portable":
        require(
            os.getenv("TRADEGRAD_REQUIRE_PORTABLE_MODEL_INTEGRATION") == "1",
            "Portable browser requires its additional hosted opt-in",
        )
    require(
        (ROOT / "frontend/node_modules/playwright/cli.js").is_file(),
        "The locked installed Playwright CLI is required",
    )
    for role in ("dispatcher", "worker"):
        h.spawn(f"browser-data-{role}", ["-m", "workers.data", role])
    require(h.base_url.endswith("/api/v1"), "Owned API origin is malformed")
    origin = h.base_url.removesuffix("/api/v1")
    source = {
        **{key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ},
        "CI": "true",
        "E2E_EMAIL": h.browser_accounts[0][0],
        "E2E_PASSWORD": h.browser_accounts[0][1],
        "E2E_OTHER_EMAIL": h.browser_accounts[1][0],
        "E2E_OTHER_PASSWORD": h.browser_accounts[1][1],
        "E2E_API_URL": origin,
        "API_PROXY_TARGET": origin,
        "E2E_MODEL_PROFILES": "1",
        "E2E_MODEL_PROFILE_ID": "hosted-synthetic",
        "PLAYWRIGHT_JUNIT_OUTPUT_FILE": str(report),
    }
    # The dedicated subreaper owns detached Chromium/Vite children. It performs
    # normal Playwright teardown before exact-pidfd bounded cleanup. No DB URL,
    # model registry/reference/key, proxy or Node injection setting is inherited.
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(ROOT / "tests/system/operator_browser_launcher.py"),
            node,
            str(ROOT),
            str(os.getpid()),
            mode,
        ],
        cwd=ROOT,
        env={
            **browser_environment(source),
            "GITHUB_ACTIONS": "true",
            "TRADEGRAD_REQUIRE_MODEL_INTEGRATION": "1",
            "TRADEGRAD_REQUIRE_MODEL_BROWSER": "1",
            **(
                {"TRADEGRAD_REQUIRE_PORTABLE_MODEL_INTEGRATION": "1"}
                if mode == "portable"
                else {}
            ),
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    h.processes.append(("owned-browser-supervisor", process))
    admit_owned_group(process)
    try:
        result = wait_owned_group(process, timeout=h.remaining(355))
    except (subprocess.TimeoutExpired, TimeoutError):
        # Ask the supervisor to finish its own bounded descendant cleanup first.
        # An outer hard timeout is failure, never browser-containment evidence.
        signal_owned_process(process, signal.SIGTERM)
        try:
            wait_owned_group(process, timeout=30)
        except (subprocess.TimeoutExpired, TimeoutError):
            pytest.fail(
                "Owned browser supervision exceeded its cleanup bound", pytrace=False
            )
        pytest.fail(
            "Owned browser acceptance exceeded its outer time bound", pytrace=False
        )
    finally:
        if process.returncode != 0:
            # The raw report stays in an owned private temporary directory.
            # Print only known repository source coordinates before its cleanup.
            print(failure_diagnostic(report, ROOT))
        if process.returncode is not None:
            # Do not leave a reaped numeric group ID for later harness cleanup.
            h.processes = [
                (label, item) for label, item in h.processes if item is not process
            ]
    require(
        result == 0, "Configured-profile Chromium case or owned-child cleanup failed"
    )
    require(
        h.server.request_count == 0, "Seed-only browser case issued a model request"
    )
