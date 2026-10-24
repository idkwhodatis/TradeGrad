"""I7 completed evidence survives a genuine cold restore and a new HTTP process.

TRADEGRAD_REQUIRE_EVIDENCE_RESTORE=1 opts into real PostgreSQL (CREATEDB),
Docker native evaluation, the durable queue and pg_dump/pg_restore. Require
TEST_DATABASE_URL, separate API_PYTHON/RESEARCH_PYTHON/DATA_PYTHON environments,
and exact-head evaluator images. PG_DUMP_EXECUTABLE/PG_RESTORE_EXECUTABLE may
select the existing restricted CI stream wrappers, never expand their allowlist.

All successful results, parity, comparisons, notes and pins are production API /
worker creations. The only optimizer run uses zero iterations to obtain an
original leaf; diagnostics and all controls use the independent evaluator. No
candidate import on the host, synthetic successful result, SQL business writes,
SQLite fallback, paid model or live provider is permitted. The market CSV is
explicitly synthetic. Opt-out / collection performs no IO to external services.
Archives, account passwords and database URLs stay private and are never printed.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager, nullcontext
from uuid import uuid4

import httpx
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url
from test_independent_pipeline import (
    TERMINAL,
    _assert_frozen_replay,
    _assert_queue_no_models_or_new_data,
    _evidence,
    _finished,
    _import_count,
    _source,
    _submit_diagnostic,
)
from test_research_optimization import ResearchProcessHarness, runtime_paths
from test_worker_recovery import ROOT, require

from backend.app.core.db import REQUIRED_SCHEMA_REVISION

DATABASE_NAME = re.compile(r"tradegrad_backup_test_[0-9a-f]{32}\Z")
GATE_SECONDS = 900
PIN_FIELDS = (
    "evaluation_id",
    "execution_attempt_id",
    "result_manifest_sha256",
    "trace_manifest_sha256",
    "source_kind",
    "source_id",
    "code_sha256",
)
TRACE_FIELDS = PIN_FIELDS[1:4]


class EvidenceRestoreHarness(ResearchProcessHarness):
    """Two independently owned databases; no change to other system harnesses."""

    def __init__(self, root, database_url, interpreters):
        super().__init__(root, database_url, interpreters)
        self.name = "tradegrad_backup_test_" + uuid4().hex
        require(
            bool(DATABASE_NAME.fullmatch(self.name)), "Invalid private DB namespace"
        )
        self.private_url = (
            make_url(self.private_url)
            .set(database=self.name)
            .render_as_string(hide_password=False)
        )
        self.env["DATABASE_URL"] = (
            make_url(self.env["DATABASE_URL"])
            .set(database=self.name)
            .render_as_string(hide_password=False)
        )
        self.env.update(
            MODEL_PROFILES_ENABLED="false", MODEL_SYNTHETIC_TEST_MODE="false"
        )
        self.deadline = time.monotonic() + GATE_SECONDS
        self.database_oid = None
        self.credentials = []

    def remaining(self, maximum):
        result = min(maximum, self.deadline - time.monotonic())
        require(result > 0, "Completed-evidence restore exceeded its 900-second budget")
        return result

    def record_identity(self):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT oid, datdba FROM pg_database WHERE datname=%s", (self.name,)
            ).fetchone()
        require(
            row is not None and row[1] == self.database_owner,
            "Private DB owner changed",
        )
        self.database_oid = row[0]

    def command(self, label, arguments, *, extra_env=None):
        # setup creates the DB immediately before its first migration command.
        # Record OID before migrations/bootstrap can fail, not just after setup.
        if self.created and self.database_oid is None:
            self.record_identity()
        super().command(label, arguments, extra_env=extra_env)

    def request(self, client, method, path, *, status=200, **kwargs):
        # These are runtime-generated fixture credentials, retained only in RAM
        # to prove an actual new login after restoring users into a fresh DB.
        # Note admission has an explicit 30-second bounded loader contract.
        # Preserve that budget rather than inheriting the recovery harness's
        # unrelated three-second status-poll timeout. The whole gate stays bounded.
        response = client.request(
            method,
            path,
            timeout=self.remaining(35 if path.startswith("/research-notes") else 10),
            **kwargs,
        )
        require(
            response.status_code == status,
            f"HTTP {method} {path} returned {response.status_code}, expected {status}",
        )
        if method == "POST" and path == "/login/access-token":
            self.credentials.append(dict(kwargs["data"]))
        return response

    def create_empty(self):
        with self.connection() as connection:
            require(
                connection.execute(
                    "SELECT 1 FROM pg_database WHERE datname=%s", (self.name,)
                ).fetchone()
                is None,
                "Fresh restore database already exists",
            )
            self.database_owner = connection.execute(
                "SELECT oid FROM pg_roles WHERE rolname=current_user"
            ).fetchone()[0]
            self.creation_attempted = True
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(self.name)
                )
            )
            self.created = True
        self.record_identity()

    def restart_api(self, credentials):
        # No bootstrap, migrations, worker, data import or rerun is allowed here.
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}/api/v1"
        self.spawn(
            "restored-api",
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
        for account in credentials:
            client = httpx.Client(base_url=self.base_url, timeout=3, trust_env=False)
            self.clients.append(client)

            def ready(client=client):
                try:
                    return client.get("/health/ready").status_code == 200
                except httpx.TransportError:
                    return False

            self.until(ready, "Restored HTTP API never became ready", timeout=20)
            token = self.request(
                client, "POST", "/login/access-token", data=account
            ).json()
            require(token.get("token_type") == "bearer", "Restored login failed")
            client.headers["Authorization"] = "Bearer " + token["access_token"]
        require(len(self.clients) == 2, "Both restored owners must authenticate")

    def cleanup(self):
        errors = []
        for client in self.clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001 -- continue independently owned cleanup
                errors.append("owned HTTP client close failed")
        try:
            self.stop_processes()
        except Exception:  # noqa: BLE001 -- never expose process diagnostics
            errors.append("owned process-group cleanup unconfirmed")
        self.credentials.clear()
        if self.creation_attempted:
            try:
                with self.connection() as connection:
                    row = connection.execute(
                        "SELECT oid, datdba FROM pg_database WHERE datname=%s",
                        (self.name,),
                    ).fetchone()
                if row is not None:
                    require(
                        bool(DATABASE_NAME.fullmatch(self.name)),
                        "Unsafe cleanup namespace",
                    )
                    require(
                        row[1] == self.database_owner,
                        "Cleanup refused unexpected DB owner",
                    )
                    # Lost CREATE replies may predate OID capture, but must still
                    # match the absent-before-create name and recorded owner.
                    require(
                        self.database_oid in (None, row[0]),
                        "Cleanup refused replaced DB OID",
                    )
                    with self.connection(private=True) as connection:
                        for table in ("execution_attempts", "research_evaluations"):
                            if connection.execute(
                                "SELECT to_regclass(%s)", (table,)
                            ).fetchone()[0]:
                                self.attempts.update(
                                    str(row[0])
                                    for row in connection.execute(
                                        sql.SQL("SELECT id FROM {}").format(
                                            sql.Identifier(table)
                                        )
                                    )
                                )
                    for identifier in self.attempts:
                        if not self.runner.stop(identifier):
                            errors.append("owned native sandbox removal unconfirmed")
                    if errors:
                        raise RuntimeError(
                            "Unconfirmed execution stop; retain private recovery database"
                        )
                    with self.connection() as connection:
                        require(
                            connection.execute(
                                "SELECT oid, datdba FROM pg_database WHERE datname=%s",
                                (self.name,),
                            ).fetchone()
                            == row,
                            "Cleanup DB identity changed",
                        )
                        # Never terminate sessions or FORCE-drop even our own DB.
                        connection.execute(
                            sql.SQL("DROP DATABASE {}").format(
                                sql.Identifier(self.name)
                            )
                        )
            except Exception:  # noqa: BLE001 -- no connection strings / SQL / credentials
                errors.append("owned disposable database cleanup failed")
        require(not errors, "; ".join(errors))


@pytest.fixture
def evidence_restore_harnesses(tmp_path):
    if os.getenv("TRADEGRAD_REQUIRE_EVIDENCE_RESTORE") != "1":
        pytest.skip("Real completed-evidence cold restore was not requested")
    database = os.getenv("TEST_DATABASE_URL")
    require(
        bool(database), "TEST_DATABASE_URL is mandatory for completed-evidence restore"
    )
    tools = []
    for setting, fallback in (
        ("PG_DUMP_EXECUTABLE", "pg_dump"),
        ("PG_RESTORE_EXECUTABLE", "pg_restore"),
    ):
        executable = os.getenv(setting, fallback)
        require(
            bool(executable and shutil.which(executable)),
            f"{setting} must be executable",
        )
        tools.append(executable)
    harnesses = []
    try:
        interpreters = runtime_paths()
        for name in ("source", "restored"):
            root = tmp_path / name
            root.mkdir(mode=0o700)
            harnesses.append(EvidenceRestoreHarness(root, database, interpreters))
        source, restored = harnesses
        source.setup()
        restored.create_empty()
        yield source, restored, tools
    except Exception as error:  # noqa: BLE001 -- sanitize subprocess/DB/HTTP failure details
        pytest.fail(
            f"Completed-evidence restore failed ({type(error).__name__})", pytrace=False
        )
    finally:
        # An error cleaning one database must not bypass the other cleanup.
        failures = []
        for harness in reversed(harnesses):
            try:
                harness.cleanup()
            except (Exception, pytest.fail.Exception):  # noqa: BLE001 -- finish both owned cleanups
                failures.append("owned evidence restore cleanup failed")
        require(not failures, "; ".join(failures))


def _pin(h, raw):
    value = {key: raw[key] for key in PIN_FIELDS}
    spec = h.request(
        h.clients[0], "GET", f"/strategy-versions/{value['source_id']}/spec"
    ).json()
    require(
        spec["code_sha256"] == value["code_sha256"], "Description changed actual code"
    )
    value.update(spec_revision=spec["revision"], spec_sha256=spec["spec_sha256"])
    return value, spec


def _trace_params(pin):
    return {key: pin[key] for key in TRACE_FIELDS}


def _control(h, source):
    parameters = source["capability"]["source_parameters"]
    require(
        parameters["target_weight"] > 0, "Source allocation must produce actual trades"
    )
    created = h.request(
        h.clients[0],
        "POST",
        "/controlled-experiments",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "source": source["pin"],
            "research_capture": {"mode": "research"},
            "max_runtime_seconds": 120,
            "name": "Cold restore real three-arm evidence",
            "hypothesis": "Zero allocation removes orders; an execution failure remains a failure.",
            "planned_change": "One declared allocation parameter, plus a separate failing code patch.",
            "arms": [
                {"name": "Exact baseline", "code": source["code"]},
                {
                    "name": "Zero allocation",
                    "code": source["code"],
                    "parameters": {**parameters, "target_weight": 0.0},
                },
                {
                    "name": "Actual isolated failure",
                    "code": "raise RuntimeError('deliberate restore gate failure')\n"
                    + source["code"],
                },
            ],
        },
    ).json()

    def complete():
        value = h.request(
            h.clients[0], "GET", f"/controlled-experiments/{created['id']}"
        ).json()
        return (
            value if all(arm["status"] in TERMINAL for arm in value["arms"]) else None
        )

    control = h.until(complete, "Real control arms did not terminate", timeout=240)
    require(
        [arm["status"] for arm in control["arms"]]
        == ["succeeded", "succeeded", "failed"],
        "No-trade/failure semantics changed",
    )
    require(
        [arm["interpretation"] for arm in control["arms"]]
        == [
            "baseline",
            "single_parameter_change_not_causal_proof",
            "not_individually_validated",
        ],
        "Control falsely promoted a code patch to single-factor proof",
    )
    require(
        control["root_experiment_id"] == source["detail"]["experiment_id"]
        and control["protocol_sha256"] == source["capability"]["protocol_sha256"]
        and control["exposure_snapshot"]["independent_unseen_holdout_established"]
        is False,
        "Control reset original fixed protocol or exposure ancestry",
    )
    arms, evidence = [], []
    for arm in control["arms"]:
        detail = _finished(h, arm["run_id"], arm["status"])
        require(
            detail is not None and len(detail["attempts"]) == 1,
            "Control lost physical attempt",
        )
        attempt = detail["attempts"][0]
        require(
            detail["config"]["source"] == source["pin"]
            and detail["config"]["protocol_sha256"] == control["protocol_sha256"],
            "Arm changed source/protocol",
        )
        pin = None
        if arm["status"] == "succeeded":
            pin, _ = _pin(h, arm["result_pin"])
            evidence.append(
                _evidence(h, pin["evaluation_id"], mode="native", independent=True)
            )
        else:
            require(
                arm["result_pin"] is None
                and arm["error"]
                and detail["result_pin"] is None,
                "Failed arm became successful zero-profit evidence",
            )
        arms.append(
            {
                "ordinal": arm["ordinal"],
                "run_id": arm["run_id"],
                "status": arm["status"],
                "generation": detail["generation"],
                "execution_attempt_id": attempt["id"],
                "result": pin,
            }
        )
    zero = evidence[1]["bundle"]
    metrics = {item["name"]: item["value"] for item in zero["metrics"]}
    require(
        not zero["orders"]["rows"]
        and not zero["fills"]["rows"]
        and not zero["closed_trades"]["rows"]
        and zero["equity"]["rows"]
        and metrics["combined_score"] is None
        and metrics["win_rate"] is None
        and metrics["return_per_trade"] is None
        and metrics["trade_per_day"] == 0,
        "Real no-trade result fabricated economics or lost account history",
    )
    require(
        len({arm["run_id"] for arm in arms}) == 3
        and len({arm["strategy_version_id"] for arm in control["arms"]}) == 3,
        "Controlled arms reused mutable identities",
    )
    return control, arms


def _comparison(h, arms):
    created = h.request(
        h.clients[0],
        "POST",
        "/comparisons",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "left": arms[0]["result"],
            "right": arms[1]["result"],
            "options": {"max_runtime_seconds": 120},
        },
    ).json()

    def complete():
        value = h.request(h.clients[0], "GET", f"/comparisons/{created['id']}").json()
        if value["status"] not in TERMINAL:
            return None
        require(
            value["status"] == "succeeded" and value["artifact_ready"],
            "Real queued comparison failed",
        )
        require(
            value["attempts"]
            and all(
                a["sandbox_stopped"] and a["finished_at"] for a in value["attempts"]
            ),
            "Comparison lacks confirmed query-stop receipt",
        )
        return value

    detail = h.until(complete, "Queued comparison did not finish", timeout=150)
    params = {"manifest_sha256": detail["manifest_sha256"]}
    root = f"/comparisons/{detail['id']}"
    result = h.request(h.clients[0], "GET", root + "/result", params=params).json()
    events = h.request(
        h.clients[0], "GET", root + "/sections/events", params=params
    ).json()
    require(
        result["manifest"]["observed_behavior"] == "changed"
        and result["manifest"]["causal_claim"] is False
        and events["items"],
        "Comparison omitted actual allocation differences or invented causality",
    )
    require(
        any(
            item["left"]
            and item["right"]
            and item["match_basis"] == "source_event"
            and item["left"].get("pre_state") is not None
            and item["right"].get("pre_state") is not None
            for item in events["items"]
        ),
        "Comparison lacks real paired source events and both prior states",
    )
    return detail, result, events


def _exact_note(note, *, export=False):
    suffix = "/export" if export else ""
    return f"/research-notes/{note['note_id']}/revisions/{note['revision']}{suffix}?revision_sha256={note['revision_sha256']}"


def _notes(h, source, spec, replay, control, arms, comparison, comparison_events):
    client = h.clients[0]
    decisions = h.request(
        client,
        "GET",
        f"/evaluations/{source['pin']['evaluation_id']}/decisions",
        params=_trace_params(source["pin"]),
    ).json()["items"]
    decision = next(
        item
        for item in decisions
        if "signal" in item["stage_ids"] and item["instrument_id"]
    )
    replay_pin, _ = _pin(h, replay["result_pin"])
    parity = h.request(
        client, "GET", f"/independent-evaluations/{replay['id']}/parity"
    ).json()
    require(
        len(parity["checks"]) == 14 and parity["outcome"] == "matched",
        "All fourteen real parity checks are required",
    )
    pair = next(
        item for item in comparison_events["items"] if item["left"] and item["right"]
    )
    evidence = [
        {
            "kind": "source",
            **{
                key: source["pin"][key]
                for key in (
                    "source_kind",
                    "source_id",
                    "code_sha256",
                    "spec_revision",
                    "spec_sha256",
                )
            },
        },
        {
            "kind": "decision",
            "leaf": source["pin"],
            "decision_id": decision["decision_id"],
            "instrument_id": decision["instrument_id"],
            "session_date": decision["session_date"],
        },
        {
            "kind": "diagnostic",
            "run_id": replay["id"],
            "config_sha256": replay["config_sha256"],
            "report_sha256": parity["report_sha256"],
            "result": replay_pin,
        },
        {
            "kind": "controlled",
            "controlled_experiment_id": control["id"],
            "request_sha256": control["request_sha256"],
            "protocol_sha256": control["protocol_sha256"],
            "arms": arms,
            "selected_arm": 1,
        },
        {
            "kind": "comparison",
            "comparison_id": comparison["id"],
            "input_sha256": comparison["input_sha256"],
            "manifest_sha256": comparison["manifest_sha256"],
            "left": arms[0]["result"],
            "right": arms[1]["result"],
            "event_pair_id": pair["event_pair_id"],
        },
    ]
    selection = {
        key: source["pin"][key]
        for key in (
            *TRACE_FIELDS,
            "evaluation_id",
            "code_sha256",
            "spec_revision",
            "spec_sha256",
        )
    }
    selection.update(
        trace_status="available",
        tab="replay",
        decision_id=decision["decision_id"],
        instrument_id=decision["instrument_id"],
        session_date=decision["session_date"],
    )
    content = {
        "title": "Frozen completed evidence survives cold restore",
        "tags": ["coldrestore", "synthetic"],
        "status": "pending",
        "hypothesis": "Zero allocation removes orders under this fixed protocol.",
        "body": "Actual isolated executions; synthetic market prices, not investment evidence.",
        "counterexamples": "One arm fails and is never included as zero-profit success.",
        "applicability": "No causal, live-provider or holdout-independence claim.",
        "evidence": evidence,
        "restore": {
            "strategy_version_id": source["pin"]["source_id"],
            "selection": selection,
        },
    }
    first = h.request(
        client,
        "POST",
        "/research-notes",
        status=201,
        headers={"Idempotency-Key": uuid4().hex},
        json={"content": content},
    ).json()
    second = h.request(
        client,
        "POST",
        f"/research-notes/{first['note_id']}/revisions",
        status=201,
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "base_revision": first["revision"],
            "base_revision_sha256": first["revision_sha256"],
            "content": {
                **content,
                "status": "mixed",
                "next_steps": "Preserve failure and investigate another window separately.",
            },
            "change_note": "Human-maintained status; profit is not verification.",
        },
    ).json()
    require(
        first["revision"] == 1 and second["revision"] == 2 and first["restore_href"],
        "Note revisions or exact decision restoration missing",
    )
    for note in (first, second):
        require(
            all(item["state"] == "available" for item in note["evidence"])
            and note["retained_pin_count"] > 10,
            "Real evidence failed note admission/retention",
        )
    # Change current human prose AFTER note creation: old notes must not float.
    rule = spec["spec"]["rules"][0]
    revised = h.request(
        client,
        "POST",
        f"/strategy-versions/{source['pin']['source_id']}/spec/revisions",
        status=201,
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "code_sha256": spec["code_sha256"],
            "base_revision": spec["revision"],
            "base_spec_sha256": spec["spec_sha256"],
            "rules": [
                {
                    "rule_id": rule["rule_id"],
                    "title": rule["title"],
                    "description": "New human explanation must not retarget historical evidence.",
                }
            ],
        },
    ).json()
    require(
        revised["revision"] > spec["revision"]
        and revised["code_sha256"] == spec["code_sha256"],
        "Prose revision mutated strategy code",
    )
    return (first, second), parity, decision


def _snapshot_http(h, source, notes, replay, control, arms, comparison, decision):
    """Read all results through original strict HTTP loaders, never raw-row stand-ins."""
    client, other = h.clients
    report_sha256 = next(
        ref["report_sha256"]
        for ref in notes[0]["content"]["evidence"]
        if ref["kind"] == "diagnostic"
    )
    requests = [
        (
            f"/strategy-versions/{source['pin']['source_id']}/spec",
            {
                "revision": source["pin"]["spec_revision"],
                "spec_sha256": source["pin"]["spec_sha256"],
            },
            False,
        ),
        (f"/independent-evaluations/{replay['id']}", {}, False),
        (
            f"/independent-evaluations/{replay['id']}/parity",
            {"report_sha256": report_sha256},
            False,
        ),
        (
            f"/independent-evaluations/{replay['id']}/parity/events",
            {"report_sha256": report_sha256},
            False,
        ),
        (f"/controlled-experiments/{control['id']}", {}, False),
        (
            f"/comparisons/{comparison['id']}/result",
            {"manifest_sha256": comparison["manifest_sha256"]},
            False,
        ),
        (
            f"/comparisons/{comparison['id']}/sections/events",
            {"manifest_sha256": comparison["manifest_sha256"]},
            False,
        ),
        (
            f"/evaluations/{source['pin']['evaluation_id']}/decisions/{decision['decision_id']}",
            _trace_params(source["pin"]),
            False,
        ),
    ]
    for pin in (
        source["pin"],
        replay["result_pin"],
        *(arm["result"] for arm in arms[:2]),
    ):
        requests.append(
            (
                f"/evaluations/{pin['evaluation_id']}/trace-manifest",
                _trace_params(pin),
                False,
            )
        )
    for arm in arms:
        requests.append((f"/independent-evaluations/{arm['run_id']}", {}, False))
        if arm["result"]:
            pin = arm["result"]
            root = f"/independent-evaluations/evaluations/{pin['evaluation_id']}/result"
            params = {key: pin[key] for key in TRACE_FIELDS[:2]}
            requests.extend([(root, params, False), (root + "/download", {}, True)])
    for note in notes:
        requests.extend(
            [(_exact_note(note), {}, False), (_exact_note(note, export=True), {}, True)]
        )
    values = []
    for path, params, binary in requests:
        response = h.request(client, "GET", path, params=params or None)
        value = response.content if binary else response.json()
        if not binary and "exposure_snapshot" in value:
            # This is a newly evaluated owner-wide exposure classification, not
            # immutable metadata. Only its observation timestamp can change.
            value["exposure_snapshot"].pop("as_of")
        values.append(value)
        h.request(other, "GET", path, params=params or None, status=404)
    for note in notes:
        reopened = h.request(client, "GET", _exact_note(note)).json()
        require(reopened == note, "Exact historical note silently changed")
        exported = h.request(client, "GET", _exact_note(note, export=True)).content
        for sha in (
            note["revision_sha256"],
            source["pin"]["result_manifest_sha256"],
            comparison["manifest_sha256"],
            report_sha256,
        ):
            require(sha.encode() in exported, "Markdown omitted physical evidence hash")
    require(
        h.request(client, "GET", "/research-notes", params={"q": "coldrestore"}).json()[
            "count"
        ]
        == 1,
        "Restored current-note keyword search lost the note",
    )
    h.request(
        client,
        "POST",
        f"/research-notes/{notes[0]['note_id']}/revisions",
        status=409,
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "base_revision": notes[0]["revision"],
            "base_revision_sha256": notes[0]["revision_sha256"],
            "content": notes[0]["content"],
        },
    )
    h.request(
        client,
        "GET",
        _exact_note(notes[0]).replace(notes[0]["revision_sha256"], "0" * 64),
        status=409,
    )
    h.request(
        client,
        "GET",
        f"/comparisons/{comparison['id']}/result",
        params={"manifest_sha256": "0" * 64},
        status=409,
    )
    h.request(
        client,
        "GET",
        f"/independent-evaluations/{replay['id']}/parity",
        params={"report_sha256": "0" * 64},
        status=409,
    )
    h.request(
        client,
        "GET",
        f"/evaluations/{source['pin']['evaluation_id']}/trace-manifest",
        params={**_trace_params(source["pin"]), "result_manifest_sha256": "0" * 64},
        status=409,
    )
    return values


def _artifacts(h, replay, control, arms):
    """Retain every byte while preserving the generic typed-result release fence."""
    from backend.app.core.config import Settings
    from backend.app.core.storage_barrier import storage_guard
    from backend.app.storage import read_verified

    expected = {replay["id"]: {**replay["result_pin"], "status": "succeeded"}}
    for public, arm in zip(control["arms"], arms, strict=True):
        require(public["run_id"] == arm["run_id"], "Controlled run identity changed")
        expected[arm["run_id"]] = {
            "evaluation_id": public["evaluation_id"],
            "execution_attempt_id": arm["execution_attempt_id"],
            "source_id": public["strategy_version_id"],
            "code_sha256": public["code_sha256"],
            "status": arm["status"],
            "result_manifest_sha256": (arm["result"] or {}).get(
                "result_manifest_sha256"
            ),
        }
    with h.connection(private=True) as connection:
        rows = connection.execute(
            "SELECT a.id, a.run_id, a.attempt_id, a.storage_key, a.sha256, a.size_bytes, "
            "a.name, r.config->>'schema_version', r.config->>'execution_mode', "
            "r.strategy_version_id, r.config->'selected_strategy'->>'sha256', r.status "
            "FROM artifacts a LEFT JOIN research_runs r ON r.id=a.run_id ORDER BY a.id"
        ).fetchall()
        pins = connection.execute(
            "SELECT revision_id, resource_id, sha256, details FROM research_note_evidence_pins "
            "WHERE resource_kind='artifact' ORDER BY revision_id, resource_id"
        ).fetchall()
    require(rows and pins, "No real artifact retention inventory exists")
    lookup = {str(row[0]): row for row in rows}
    for _, identifier, sha, details in pins:
        row = lookup[str(identifier)]
        require(
            sha == row[4]
            and details["storage_key"] == row[3]
            and details["size_bytes"] == row[5],
            "Note pin does not retain exact registered physical bytes",
        )
    seen, downloads = {identifier: set() for identifier in expected}, []
    root = h.root / "storage"
    max_bytes = int(
        h.env.get(
            "MAX_ARTIFACT_BYTES", Settings.model_fields["max_artifact_bytes"].default
        )
    )
    with storage_guard(root):
        for (
            identifier,
            run,
            attempt,
            key,
            sha,
            size,
            name,
            schema,
            mode,
            source,
            code,
            status,
        ) in rows:
            pin = expected.get(str(run))
            suffix = name
            if pin is not None:
                prefix = f"evaluation-{pin['evaluation_id']}-"
                require(
                    schema == "tradegrad.independent-evaluation.v1"
                    and mode == "independent_evaluation"
                    and str(attempt) == pin["execution_attempt_id"]
                    and str(source) == pin["source_id"]
                    and code == pin["code_sha256"]
                    and status == pin["status"]
                    and (
                        name.startswith(prefix) or name == "execution-environment.json"
                    ),
                    "Independent artifact differs from its exact execution identity",
                )
                suffix = name.removeprefix(prefix)
                seen[str(run)].add(suffix)
                if suffix == "result-bundle.json":
                    require(
                        status == "succeeded" and sha == pin["result_manifest_sha256"],
                        "Independent artifact differs from the admitted result pin",
                    )
            # Registration does not authorize generic download. Independent native
            # publication also stores a legacy metrics sidecar; only the typed
            # result bundle may release those economics. Verify its physical
            # bytes as well, never skip it or relax the production loader.
            stored = read_verified(root, key, sha, max_bytes)
            require(len(stored) == size, "Registered artifact physical size changed")
            fenced = (
                pin is not None and status == "succeeded" and suffix == "metrics.json"
            )
            response = h.request(
                h.clients[0],
                "GET",
                f"/artifacts/{identifier}/download",
                status=409 if fenced else 200,
            )
            code = None
            if fenced:
                code = response.json().get("code")
                require(
                    code == "typed_final_result_required",
                    "Registered metrics failed for an unexpected admission reason",
                )
            else:
                require(
                    hashlib.sha256(response.content).hexdigest() == sha
                    and len(response.content) == size,
                    "Registered artifact failed original strict download admission",
                )
            downloads.append((identifier, response.status_code, code))
            h.request(
                h.clients[1], "GET", f"/artifacts/{identifier}/download", status=404
            )
    for identifier, pin in expected.items():
        required = {"execution-environment.json", "provenance.json", "logs.txt"}
        if pin["status"] == "succeeded":
            required.update({"metrics.json", "result-bundle.json"})
        require(
            required <= seen[identifier],
            "Independent execution lost required result or failure diagnostic artifacts",
        )
    return rows, pins, downloads


def _quiesce(h, comparison):
    def drained():
        with h.connection(private=True) as connection:
            return (
                connection.execute(
                    "SELECT (SELECT count(*) FROM procrastinate_jobs WHERE status IN ('todo','doing')) + "
                    "(SELECT count(*) FROM outbox_events WHERE dispatched_at IS NULL) + "
                    "(SELECT count(*) FROM data_outbox WHERE dispatched_at IS NULL)"
                ).fetchone()[0]
                == 0
            )

    h.until(drained, "Actual durable queues never drained", timeout=30)
    owned = [process for _, process in h.processes]
    h.stop_processes()
    require(
        not h.processes
        and not h.expected_alive
        and all(
            getattr(process, "_tradegrad_group", {}).get("verified", False)
            for process in owned
        ),
        "An owned API/worker/process group survived cold shutdown",
    )
    with h.connection(private=True) as connection:
        leaves = connection.execute(
            "SELECT id, status, sandbox_stopped, finished_at FROM research_evaluations"
        ).fetchall()
        attempts = connection.execute(
            "SELECT id, status, sandbox_stopped, finished_at FROM execution_attempts"
        ).fetchall()
    require(
        leaves
        and attempts
        and all(row[1] in TERMINAL and row[2] and row[3] for row in leaves + attempts),
        "Cold shutdown has unconfirmed leaf/attempt stop receipts",
    )
    for row in leaves + attempts:
        require(
            h.runner._inspect(str(row[0])) is None,
            "Owned native container survived cold shutdown",
        )
    from backend.app.comparison_guard import comparison_query_confirmed_stopped

    for attempt in comparison["attempts"]:
        require(
            comparison_query_confirmed_stopped(
                h.root / "storage", comparison["run_id"], attempt["id"]
            ),
            "Comparison live query lock survived cold shutdown",
        )


@contextmanager
def _cold_database(database, storage_root=None):
    """Retain production PG exclusion and filesystem read guard through the copy.

    Prove EX root availability, then downgrade that same open descriptor to SH
    so the production backup's nested SH reader can enter. Linux flock conversion
    is not promised atomic: stopped owned writers plus retained PG EX admission
    exclusion protect that conversion; SH then remains held through the copy.
    Restore creates its absent root and holds its own production SH guard.
    """
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from backend.app.core.maintenance import MaintenanceBusy, _lock
    from backend.app.core.storage_barrier import storage_guard

    engine = database._inspection_engine()
    try:
        with engine.connect() as connection:
            _lock(connection, exclusive=True)
            pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            # A different real ORM transaction cannot bypass cold exclusion.
            with pytest.raises(MaintenanceBusy), Session(engine) as probe:
                probe.execute(text("SELECT 1"))
            root_guard = (
                storage_guard(storage_root, exclusive=True)
                if storage_root
                else nullcontext()
            )
            with root_guard as descriptor:
                if storage_root is not None:
                    with pytest.raises(MaintenanceBusy), storage_guard(storage_root):
                        pytest.fail(
                            "Filesystem writer bypassed exclusive cold guard",
                            pytrace=False,
                        )
                    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                yield
                require(
                    not connection.invalidated
                    and connection.in_transaction()
                    and connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
                    == pid,
                    "Maintenance PG guard was lost or connection changed",
                )
    finally:
        engine.dispose()


def _run_completed_evidence_restore(evidence_restore_harnesses):
    h, restored, executables = evidence_restore_harnesses
    source = _source(h, "native", maintained=True)
    source["pin"], spec = _pin(h, source["pin"])
    source["capability"] = h.request(
        h.clients[0], "POST", "/independent-evaluations/capability", json=source["pin"]
    ).json()
    import_count = _import_count(h)
    created = _submit_diagnostic(h, source, {"mode": "research"})
    replay = h.until(
        lambda: _finished(h, created["id"]),
        "Real matched diagnostic did not finish",
        timeout=150,
    )
    _assert_frozen_replay(h, source, replay, "native", matched=True)
    control, arms = _control(h, source)
    comparison, comparison_result, events = _comparison(h, arms)
    notes, parity, decision = _notes(
        h, source, spec, replay, control, arms, comparison, events
    )
    _assert_queue_no_models_or_new_data(
        h, [replay["id"], *(arm["run_id"] for arm in arms)], import_count
    )
    with h.connection(private=True) as connection:
        require(
            connection.execute(
                "SELECT count(*) FROM outbox_events o JOIN procrastinate_jobs j ON j.id=o.job_id "
                "WHERE o.run_id=%s AND j.status IN ('doing','succeeded')",
                (comparison["run_id"],),
            ).fetchone()[0]
            == 1,
            "Comparison bypassed durable queue",
        )
    # Generic diagnostic-log downloads conservatively record full-dataset
    # exposure. Perform them before freezing the owner-wide exposure response.
    inventory = _artifacts(h, replay, control, arms)
    before = _snapshot_http(
        h, source, notes, replay, control, arms, comparison, decision
    )
    _quiesce(h, comparison)

    from backend.app.operations_backup import (
        BackupLimits,
        PgTools,
        PostgresDatabase,
        create_backup,
        restore_backup,
        verify_backup,
    )

    ref = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
    )
    require(
        ref.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}\n", ref.stdout),
        "Exact application head unavailable",
    )
    application_ref = ref.stdout.strip()
    source_database = PostgresDatabase(h.private_url)
    target_database = PostgresDatabase(restored.private_url)
    limits = BackupLimits(timeout_seconds=120)
    pg_tools = PgTools(pg_dump=(executables[0],), pg_restore=(executables[1],))
    archive = h.root.parent / "private-cold-backup"
    with _cold_database(source_database, h.root / "storage"):
        manifest = create_backup(
            database=source_database,
            storage_root=h.root / "storage",
            destination=archive,
            durable_parent=h.root.parent,
            application_ref=application_ref,
            quiesced=True,
            tools=pg_tools,
            limits=limits,
        )
    require(
        manifest["schema_revision"] == REQUIRED_SCHEMA_REVISION
        and verify_backup(archive, application_ref=application_ref, limits=limits)
        == manifest,
        "Real cold archive failed complete manifest verification",
    )
    with (archive / "database.dump").open("rb") as dump:
        require(
            dump.read(5) == b"PGDMP", "Backup is not an actual PostgreSQL custom dump"
        )
    with _cold_database(target_database):
        result = restore_backup(
            database=target_database,
            backup_root=archive,
            storage_root=restored.root / "storage",
            durable_parent=restored.root,
            application_ref=application_ref,
            quiesced=True,
            trust_backup=True,
            tools=pg_tools,
            limits=limits,
        )
    require(
        result == manifest
        and h.name != restored.name
        and not (restored.root / "storage" / ".restore-incomplete").exists(),
        "Fresh restore never completed",
    )
    # The original remains stopped and untouched; only the fresh API restarts.
    restored.restart_api(list(h.credentials))
    require(
        _snapshot_http(
            restored, source, notes, replay, control, arms, comparison, decision
        )
        == before,
        "Cold-restored HTTP evidence silently changed physical pins, provenance, or result semantics",
    )
    require(
        _artifacts(restored, replay, control, arms) == inventory,
        "Restored artifact registration/retention pins differ",
    )
    restored_parity = restored.request(
        restored.clients[0], "GET", f"/independent-evaluations/{replay['id']}/parity"
    ).json()
    restored_comparison = restored.request(
        restored.clients[0],
        "GET",
        f"/comparisons/{comparison['id']}/result",
        params={"manifest_sha256": comparison["manifest_sha256"]},
    ).json()
    require(
        restored_parity == parity and restored_comparison == comparison_result,
        "Original matched-parity or comparison manifest hash changed after restore",
    )
    _assert_queue_no_models_or_new_data(
        restored, [replay["id"], *(arm["run_id"] for arm in arms)], import_count
    )
    require(not h.processes, "Restoring results restarted an original writer")


def test_real_completed_research_evidence_cold_restore_reopens_exact_http(
    evidence_restore_harnesses,
):
    try:
        _run_completed_evidence_restore(evidence_restore_harnesses)
    except Exception as error:  # noqa: BLE001 -- never serialize archive, result, credentials, or HTTP payloads
        pytest.fail(
            f"Completed-evidence HTTP restore failed ({type(error).__name__})",
            pytrace=False,
        )
