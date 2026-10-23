"""Hosted-only acceptance: SIGKILL real queue workers, then recover through HTTP.

Opt in with TRADEGRAD_REQUIRE_PROCESS_RECOVERY=1 and TEST_DATABASE_URL. The
PostgreSQL role must be able to create a disposable database, and the local
Docker evaluator image must already exist. There are no test HTTP routes,
mocked services, direct business-state writes, or local fallback runtimes.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from runtimes.evaluator.runner import DockerEvaluator
from script.ci_owned_group import (
    admit_owned_group,
    peek_owned_group,
    signal_owned_group,
    stop_owned_groups,
    wait_owned_group,
)
from script.ci_recovery_diagnostics import recovery_diagnostic

ROOT = Path(__file__).resolve().parents[2]
SLOW_SEED = "E2E slow cn_time seed (20s import delay)"


def require(condition, message):
    """Do not let assertion introspection serialize credentials/HTTP bodies."""
    if not condition:
        pytest.fail(message, pytrace=False)


class ProcessHarness:
    def __init__(self, root: Path, database_url: str):
        self.deadline = time.monotonic() + 120
        self.root = root
        self.name = "tradegrad_process_" + uuid4().hex
        self.created = False
        self.creation_attempted = False
        self.database_owner = None
        self.processes = []
        self.expected_alive = set()
        self.clients = []
        self.attempts = set()
        self.runs = []
        self.runner = DockerEvaluator(state_dir=root / "runner")
        url = make_url(database_url)
        require(url.get_backend_name() == "postgresql", "Real PostgreSQL is required")
        self.admin_url = url.set(drivername="postgresql").render_as_string(
            hide_password=False
        )
        # Connection-string query fields must not redirect the private database.
        private = url.difference_update_query(("dbname", "database", "options"))
        self.private_url = private.set(
            drivername="postgresql", database=self.name
        ).render_as_string(hide_password=False)
        self.env = {
            **os.environ,
            "DATABASE_URL": private.set(
                drivername="postgresql+psycopg", database=self.name
            ).render_as_string(hide_password=False),
            "SECRET_KEY": secrets.token_urlsafe(48),
            "STORAGE_ROOT": str(root / "storage"),
            "TRADEGRAD_RUNNER_STATE_DIR": str(root / "runner"),
            "LEASE_SECONDS": "10",
            "HEARTBEAT_SECONDS": "1",
            "WORKER_CONCURRENCY": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def remaining(self, maximum):
        remaining = min(maximum, self.deadline - time.monotonic())
        require(remaining > 0, "Real-process recovery exceeded its 120-second budget")
        return remaining

    def connection(self, *, private=False):
        connection = psycopg.connect(
            self.private_url if private else self.admin_url,
            autocommit=True,
            connect_timeout=5,
            options="-cstatement_timeout=10000 -clock_timeout=5000",
        )
        if (
            private
            and connection.execute("SELECT current_database()").fetchone()[0]
            != self.name
        ):
            connection.close()
            raise RuntimeError("Private database identity mismatch")
        return connection

    def spawn(self, label, arguments, *, persistent=True, extra_env=None):
        process = subprocess.Popen(
            [sys.executable, *arguments],
            cwd=ROOT,
            env={**self.env, **(extra_env or {})},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.processes.append((label, process))
        admit_owned_group(process)
        if persistent:
            self.expected_alive.add(process)
        return process

    def command(self, label, arguments, *, extra_env=None):
        process = self.spawn(label, arguments, persistent=False, extra_env=extra_env)
        require(
            wait_owned_group(process, timeout=self.remaining(30)) == 0,
            f"Owned {label} command failed; child output is intentionally suppressed",
        )

    def alive(self):
        for label, process in self.processes:
            if process in self.expected_alive:
                require(
                    peek_owned_group(process) is None,
                    f"Owned {label} process exited early",
                )

    def until(self, predicate, message, *, timeout, diagnostics=None):
        deadline = time.monotonic() + self.remaining(timeout)
        while time.monotonic() < deadline:
            self.alive()
            result = predicate()
            if result:
                return result
            time.sleep(0.15)
        if diagnostics is not None:
            # Fixed enums/numeric timing only, no UUIDs, credentials, SQL, logs,
            # event payloads or arbitrary HTTP/database error messages.
            message += "; " + json.dumps(diagnostics(), sort_keys=True, allow_nan=False)
        pytest.fail(message, pytrace=False)

    def recovery_diagnostics(self, old):
        snapshots = []
        for snapshot in old:
            row = None
            container_state = "unknown"
            try:
                with self.connection(private=True) as connection:
                    connection.execute("SET statement_timeout = 1000")
                    row = connection.execute(
                        """SELECT r.status, a.status,
                          extract(epoch FROM (a.lease_expires_at-clock_timestamp())),
                          extract(epoch FROM (clock_timestamp()-a.heartbeat_at)),
                          a.sandbox_stopped, a.finished_at IS NOT NULL,
                          EXISTS(SELECT 1 FROM run_events e
                            WHERE e.attempt_id=a.id AND e.type='recovery_blocked')
                        FROM execution_attempts a JOIN research_runs r ON r.id=a.run_id
                        WHERE a.id=%s""",
                        (UUID(snapshot["attempt_id"]),),
                    ).fetchone()
            except Exception:  # noqa: BLE001 - never serialize diagnostic failures
                row = None
            try:
                state = self.runner._inspect(snapshot["attempt_id"])
                container_state = (
                    "absent"
                    if state is None
                    else "running"
                    if state["State"]["Running"]
                    else "exited"
                )
            except Exception:  # noqa: BLE001 - unavailable is not absence
                container_state = "unknown"
            snapshots.append(recovery_diagnostic(row, container_state=container_state))
        return {"recovery_observations": snapshots}

    def request(self, client, method, path, *, status=200, **kwargs):
        response = client.request(method, path, timeout=self.remaining(3), **kwargs)
        require(
            response.status_code == status,
            f"HTTP {method} {path} returned {response.status_code}, expected {status}",
        )
        return response

    def detail(self, run):
        return self.request(run["client"], "GET", f"/runs/{run['id']}").json()

    def start_workers(self):
        return [
            self.spawn(f"worker-{index}", ["-m", "workers.research", "worker"])
            for index in range(2)
        ]

    def setup(self):
        from backend.app.core.atomic_storage import provision_directory

        provision_directory(self.root / "storage", durable_parent=self.root)
        require(
            self.runner.available(), "Required real Docker evaluator is unavailable"
        )
        with self.connection() as connection:
            require(
                connection.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (self.name,)
                ).fetchone()
                is None,
                "Generated disposable database name unexpectedly exists",
            )
            self.database_owner = connection.execute(
                "SELECT oid FROM pg_roles WHERE rolname = current_user"
            ).fetchone()[0]
            self.creation_attempted = True
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(self.name)
                )
            )
            self.created = True
        self.command(
            "migration",
            ["-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"],
        )
        accounts = []
        for index in range(2):
            account = {
                "email": f"recovery-{uuid4().hex}-{index}@example.com",
                "password": secrets.token_urlsafe(32),
            }
            self.command(
                "bootstrap",
                [
                    "-m",
                    "backend.app.bootstrap",
                    "--email",
                    account["email"],
                    "--password-env",
                    "PROCESS_TEST_PASSWORD",
                    "--with-synthetic-fixtures",
                    "--with-e2e-fixtures",
                ],
                extra_env={"PROCESS_TEST_PASSWORD": account["password"]},
            )
            accounts.append(account)
        # The socket allocation happens only after the explicit hosted opt-in.
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}/api/v1"
        self.spawn(
            "api",
            [
                "-m",
                "uvicorn",
                "backend.app.asgi:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
        )
        for role in ("dispatcher", "reconciler"):
            self.spawn(role, ["-m", "workers.research", role])
        self.workers = self.start_workers()
        for account in accounts:
            client = httpx.Client(base_url=self.base_url, timeout=3, trust_env=False)
            self.clients.append(client)

            def ready(client=client):
                try:
                    return client.get("/health/ready").status_code == 200
                except httpx.TransportError:
                    return False

            self.until(ready, "Owned HTTP API did not become ready", timeout=15)
            token = self.request(
                client,
                "POST",
                "/login/access-token",
                data={
                    "username": account["email"],
                    "password": account.pop("password"),
                },
            ).json()
            require(
                token.get("token_type") == "bearer", "Invalid authentication metadata"
            )
            client.headers["Authorization"] = "Bearer " + token["access_token"]

    def submit(self):
        for index, client in enumerate(self.clients):
            datasets = self.request(client, "GET", "/datasets").json()["data"]
            strategies = self.request(client, "GET", "/strategy-versions").json()[
                "data"
            ]
            dataset = next(item for item in datasets if item["market"] == "CN")
            strategy = next(item for item in strategies if item["name"] == SLOW_SEED)
            config = {
                "task": "cn_time",
                "profile": "corrected_explicit_config",
                "dataset_version_id": dataset["id"],
                "strategy_version_id": strategy["id"],
                # Distinct requests make crossed namespaces detectable, not just IDs.
                "start_date": (
                    date.fromisoformat(dataset["start_date"])
                    + timedelta(days=index * 7)
                ).isoformat(),
                "end_date": dataset["end_date"],
            }
            response = self.request(
                client,
                "POST",
                "/runs",
                status=202,
                json=config,
                headers={"Idempotency-Key": str(uuid4())},
            ).json()
            self.runs.append({"id": response["id"], "client": client, "config": config})
        require(
            self.runs[0]["id"] != self.runs[1]["id"], "Concurrent runs reused an ID"
        )

    def running(self, generation):
        snapshots = []
        for run in self.runs:
            detail = self.detail(run)
            require(
                detail["status"]
                not in {"failed", "cancelled", "interrupted", "succeeded"},
                "Run finished before both real sandboxes were concurrently running",
            )
            if detail["status"] != "running" or not detail["active_attempt_id"]:
                return False
            attempt_id = str(UUID(detail["active_attempt_id"]))
            self.attempts.add(attempt_id)
            state = self.runner._inspect(attempt_id)
            if state is None or not state["State"]["Running"]:
                return False
            require(detail["generation"] == generation, "Unexpected attempt generation")
            require(
                len(detail["attempts"]) == generation, "Duplicate attempt was created"
            )
            labels = state["Config"]["Labels"]
            require(
                labels["io.tradegrad.run_id"] == run["id"],
                "Sandbox belongs to another run",
            )
            require(
                labels["io.tradegrad.attempt_id"] == attempt_id,
                "Sandbox attempt label mismatch",
            )
            source = self.root / "storage" / "runs" / run["id"] / attempt_id / "input"
            mounts = [
                item for item in state["Mounts"] if item["Destination"] == "/input"
            ]
            require(
                len(mounts) == 1 and mounts[0]["Source"] == str(source),
                "Sandbox input namespaces crossed",
            )
            request = json.loads((source / "request.json").read_text())
            require(
                request["start_date"] == run["config"]["start_date"],
                "Staged run date window crossed",
            )
            data_path = source / "data" / "INDEX.csv"
            with data_path.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            require(
                bool(rows)
                and rows[0]["date"] == run["config"]["start_date"]
                and all(
                    run["config"]["start_date"]
                    <= row["date"]
                    < run["config"]["end_date"]
                    for row in rows
                ),
                "Staged market data does not match its own exclusive run window",
            )
            snapshots.append(
                {
                    "attempt_id": attempt_id,
                    "input_sha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
                    "started_at": state["State"]["StartedAt"],
                    "observed_at": datetime.now(UTC),
                }
            )
        # Prove overlap rather than observing one finished container followed by
        # another: the second's current running interval began before we observed
        # the first running. If startup straddled the observations, poll again.
        if (
            datetime.fromisoformat(snapshots[1]["started_at"])
            > snapshots[0]["observed_at"]
        ):
            return False
        require(
            len({item["attempt_id"] for item in snapshots}) == 2,
            "Concurrent runs shared an attempt",
        )
        require(
            len({item["input_sha256"] for item in snapshots}) == 2,
            "Distinct run windows received identical staged data",
        )
        return snapshots

    def crash_workers(self):
        # Signal both owned process groups before waiting: no graceful finally blocks.
        for process in self.workers:
            require(
                peek_owned_group(process) is None,
                "Worker exited before fault injection",
            )
            self.expected_alive.remove(process)
            signal_owned_group(process, signal.SIGKILL)
        for process in self.workers:
            require(
                wait_owned_group(process, timeout=self.remaining(5)) == -signal.SIGKILL,
                "Worker was not killed with SIGKILL",
            )

    def reconciled(self, old):
        for run, snapshot in zip(self.runs, old, strict=True):
            detail = self.detail(run)
            if detail["status"] != "interrupted":
                require(
                    detail["status"] in {"running", "preparing"},
                    "Unexpected crash recovery status",
                )
                return False
            require(
                len(detail["attempts"]) == 1 and detail["generation"] == 1,
                "Recovery created an unsolicited attempt",
            )
            attempt = detail["attempts"][0]
            require(
                attempt["id"] == snapshot["attempt_id"] and attempt["sandbox_stopped"],
                "Recovery did not fence the old attempt",
            )
            require(
                attempt["status"] == "interrupted" and attempt["finished_at"],
                "Recovery result is not durable",
            )
            require(
                self.runner._inspect(snapshot["attempt_id"]) is None,
                "Old sandbox survived reconciliation",
            )
            # The orphan was removed while its 20-second import delay was still active.
            started = datetime.fromisoformat(snapshot["started_at"])
            require(
                (
                    datetime.fromisoformat(attempt["finished_at"]) - started
                ).total_seconds()
                < 20,
                "Orphan cleanup did not beat natural slow-seed completion",
            )
        return True

    def succeeded(self):
        for run in self.runs:
            detail = self.detail(run)
            require(
                detail["status"] not in {"failed", "cancelled", "interrupted"},
                "Resumed real evaluation did not succeed",
            )
            if detail["status"] != "succeeded":
                return False
        return True

    def verify_artifacts(self, old, new):
        all_ids = set()
        for index, run in enumerate(self.runs):
            detail = self.detail(run)
            require(
                old[index]["input_sha256"] == new[index]["input_sha256"],
                "Resuming immutable inputs changed staged data",
            )
            require(
                detail["generation"] == 2 and len(detail["attempts"]) == 2,
                "Retry history was lost or duplicated",
            )
            attempts = {item["id"]: item for item in detail["attempts"]}
            require(
                attempts[old[index]["attempt_id"]]["status"] == "interrupted",
                "Old attempt was overwritten",
            )
            require(
                attempts[new[index]["attempt_id"]]["status"] == "succeeded",
                "New attempt did not complete",
            )
            require(
                all(item["sandbox_stopped"] for item in attempts.values()),
                "Completed attempts lack stop confirmation",
            )
            require(
                detail["config"]["start_date"] == run["config"]["start_date"],
                "Persisted run configs crossed",
            )
            other = self.clients[1 - index]
            self.request(other, "GET", f"/runs/{run['id']}", status=404)
            artifacts = self.request(
                run["client"], "GET", f"/runs/{run['id']}/artifacts"
            ).json()["data"]
            require(
                {item["name"] for item in artifacts}
                == {"metrics.json", "logs.txt", "provenance.json"},
                "Successful artifact set is incomplete",
            )
            for item in artifacts:
                require(
                    item["run_id"] == run["id"]
                    and item["attempt_id"] == new[index]["attempt_id"],
                    "Artifact ownership/attempt namespaces crossed",
                )
                require(item["id"] not in all_ids, "Runs shared an artifact ID")
                all_ids.add(item["id"])
                path = f"/artifacts/{item['id']}/download"
                content = self.request(run["client"], "GET", path).content
                require(
                    len(content) == item["size_bytes"]
                    and hashlib.sha256(content).hexdigest() == item["sha256"],
                    "Downloaded artifact hash/size mismatch",
                )
                self.request(other, "GET", path, status=404)
                if item["name"] == "metrics.json":
                    envelope = json.loads(content)
                    require(
                        envelope["metrics"] == detail["result"]["metrics"],
                        "Downloaded metrics belong to a different run",
                    )
                    require(
                        envelope["market"] == "CN"
                        and envelope["profile"] == run["config"]["profile"],
                        "Engine result metadata does not match the run",
                    )
                    if index == 0:
                        expected = json.loads(
                            (ROOT / "tests/baseline/expected.json").read_text()
                        )["cn_time"]
                        require(
                            envelope["metrics"] == expected["metrics"],
                            "Full-window resumed seed changed the original engine result",
                        )
            require(
                self.runner._inspect(new[index]["attempt_id"]) is None,
                "Completed retry container remains present",
            )

    def stop_processes(self, *, initial_signal=signal.SIGTERM):
        """Stop only admitted roots; retain failed identities for cleanup retry."""
        try:
            stop_owned_groups(
                [process for _, process in self.processes],
                initial_signal=initial_signal,
            )
        finally:
            stopped = {
                process
                for _, process in self.processes
                if getattr(process, "_tradegrad_group", {}).get("verified", False)
            }
            self.processes = [
                (label, process)
                for label, process in self.processes
                if process not in stopped
            ]
            self.expected_alive.difference_update(stopped)

    def cleanup(self):
        errors = []
        for client in self.clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - process/database cleanup must still run
                errors.append("owned HTTP client close failed")
        try:
            self.stop_processes()
        except RuntimeError:
            errors.append("owned process/session cleanup is unconfirmed")
        if self.creation_attempted:
            # Reconcile a lost CREATE response. The name was absent immediately
            # before this harness attempted creation and is a random UUID name.
            # Never drop a database with an unexpected owner, even on cleanup.
            self.created = False
            try:
                with self.connection() as connection:
                    record = connection.execute(
                        "SELECT datdba FROM pg_database WHERE datname = %s",
                        (self.name,),
                    ).fetchone()
                    if record is not None:
                        if record[0] == self.database_owner:
                            self.created = True
                        else:
                            errors.append(
                                "disposable database has unexpected ownership"
                            )
            except Exception:  # noqa: BLE001 - uncertain DB state must remain explicit
                errors.append("could not reconcile disposable database creation")
        if self.created:
            # Read-only discovery also catches attempts created after a timed-out HTTP
            # request. This entire fresh DB is owned here; never query another DB.
            try:
                with self.connection(private=True) as connection:
                    exists = connection.execute(
                        "SELECT to_regclass('execution_attempts')"
                    ).fetchone()[0]
                    if exists:
                        self.attempts.update(
                            str(row[0])
                            for row in connection.execute(
                                "SELECT id FROM execution_attempts"
                            )
                        )
            except Exception:  # noqa: BLE001 - continue all cleanup without diagnostic payloads
                errors.append("could not discover owned attempts during cleanup")
        for attempt_id in self.attempts:
            if not self.runner.stop(attempt_id):
                errors.append("owned sandbox removal could not be confirmed")
        if self.created:
            try:
                with self.connection() as connection:
                    connection.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                            sql.Identifier(self.name)
                        )
                    )
            except Exception:  # noqa: BLE001 - continue all cleanup without diagnostic payloads
                errors.append("owned disposable database removal failed")
        require(not errors, "; ".join(errors))


@pytest.fixture
def process_harness(tmp_path):
    if os.getenv("TRADEGRAD_REQUIRE_PROCESS_RECOVERY") != "1":
        pytest.skip("Hosted real-process recovery gate was not explicitly requested")
    value = os.getenv("TEST_DATABASE_URL")
    require(bool(value), "TEST_DATABASE_URL is required for the hosted process gate")
    harness = None
    try:
        harness = ProcessHarness(tmp_path, value)
        harness.setup()
        yield harness
    except Exception as exc:  # noqa: BLE001 - sanitize setup failures before test reporting
        # Do not report connection strings, environment mappings or HTTP payloads.
        pytest.fail(
            f"Real-process harness failed ({type(exc).__name__})", pytrace=False
        )
    finally:
        if harness is not None:
            harness.cleanup()


def test_two_concurrent_runs_recover_after_real_workers_are_sigkilled(process_harness):
    harness = process_harness
    harness.submit()
    old = harness.until(
        lambda: harness.running(1),
        "Both original containers did not run concurrently",
        timeout=20,
    )
    harness.crash_workers()
    for run in harness.runs:
        harness.request(run["client"], "POST", f"/runs/{run['id']}/resume", status=409)
    harness.until(
        lambda: harness.reconciled(old),
        "Expired workers were not durably reconciled",
        timeout=18,
        diagnostics=lambda: harness.recovery_diagnostics(old),
    )
    harness.workers = harness.start_workers()
    for run in harness.runs:
        resumed = harness.request(
            run["client"], "POST", f"/runs/{run['id']}/resume", status=202
        ).json()
        require(resumed["generation"] == 2, "HTTP resume did not increment generation")
    new = harness.until(
        lambda: harness.running(2),
        "Both resumed containers did not run concurrently",
        timeout=20,
    )
    require(
        {item["attempt_id"] for item in old}.isdisjoint(
            item["attempt_id"] for item in new
        ),
        "Retry reused an old attempt namespace",
    )
    harness.until(
        harness.succeeded, "Resumed native evaluations did not finish", timeout=60
    )
    harness.verify_artifacts(old, new)
