"""Inert publisher/loader and hosted-audit contracts, not PG/Docker restore proof.

Real bounded files and production storage guards are used. Persistence, HTTP and
independent source/admission are explicit doubles; no strategy source executes.
"""

import ast
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from backend.app import independent_services, result_access, storage
from backend.app.core.maintenance import MaintenanceBusy
from backend.app.core.storage_barrier import storage_guard
from backend.app.models import Artifact, Evaluation, ResearchRun
from backend.app.services import DomainError
from backend.tests.test_independent_contracts import config_and_files
from workers.research.executor import publish_artifacts


def audit_helper():
    # Execute only the checked-in audit helper, never import the hosted process
    # harness or candidate source. This is the actual helper, not a reimplementation.
    path = (
        Path(__file__).resolve().parents[2]
        / "tests/system/test_research_evidence_restore.py"
    )
    module = ast.parse(path.read_text())
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_artifacts"
    )

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    namespace = {"require": require, "hashlib": hashlib}
    exec(  # noqa: S102 - checked-in helper AST only, never source supplied by a strategy
        compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"),
        namespace,
    )
    return namespace[function.name]


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    root.mkdir()
    replay_pin = {
        "evaluation_id": str(uuid4()),
        "execution_attempt_id": str(uuid4()),
        "source_id": str(uuid4()),
        "code_sha256": "a" * 64,
        "result_manifest_sha256": hashlib.sha256(b"inert typed result").hexdigest(),
    }
    replay = {"id": str(uuid4()), "result_pin": replay_pin}
    failed = {
        "run_id": str(uuid4()),
        "evaluation_id": str(uuid4()),
        "strategy_version_id": str(uuid4()),
        "code_sha256": "b" * 64,
        "status": "failed",
    }
    arms = [
        {
            "run_id": failed["run_id"],
            "execution_attempt_id": str(uuid4()),
            "status": "failed",
            "result": None,
        }
    ]
    rows, responses = [], {}
    for run, pin in (
        (replay["id"], {**replay_pin, "status": "succeeded"}),
        (
            failed["run_id"],
            {
                **failed,
                "source_id": failed["strategy_version_id"],
                "execution_attempt_id": arms[0]["execution_attempt_id"],
            },
        ),
    ):
        names = ["execution-environment.json", "provenance.json", "logs.txt"]
        if pin["status"] == "succeeded":
            names += ["metrics.json", "result-bundle.json"]
        for name in names:
            identifier = uuid4()
            raw = (
                b"inert typed result" if name == "result-bundle.json" else name.encode()
            )
            key = str(identifier)
            (root / key).write_bytes(raw)
            rows.append(
                [
                    identifier,
                    UUID(run),
                    UUID(pin["execution_attempt_id"]),
                    key,
                    hashlib.sha256(raw).hexdigest(),
                    len(raw),
                    name
                    if name == "execution-environment.json"
                    else f"evaluation-{pin['evaluation_id']}-{name}",
                    "tradegrad.independent-evaluation.v1",
                    "independent_evaluation",
                    UUID(pin["source_id"]),
                    pin["code_sha256"],
                    pin["status"],
                ]
            )
            responses[identifier] = (
                (409, b"", "typed_final_result_required")
                if name == "metrics.json"
                else (200, raw, None)
            )
    pins = [
        (
            uuid4(),
            row[0],
            row[4],
            {"storage_key": row[3], "size_bytes": row[5]},
        )
        for row in rows
    ]
    queries, requests, reads = [], [], []

    def execute(query):
        queries.append(query)
        if "FROM artifacts a" in query:
            return SimpleNamespace(fetchall=lambda: rows)
        assert "FROM research_note_evidence_pins" in query
        return SimpleNamespace(fetchall=lambda: pins)

    owner, other = object(), object()
    h = SimpleNamespace(
        root=tmp_path,
        env={},
        clients=[owner, other],
        connection=lambda **kwargs: nullcontext(SimpleNamespace(execute=execute)),
        foreign_status=404,
    )

    def request(client, method, path, *, status=200):
        assert method == "GET"
        identifier = UUID(path.split("/")[2])
        requests.append((identifier, client is other, status))
        actual, content, code = (
            (h.foreign_status, b"", "not_found")
            if client is other
            else responses[identifier]
        )
        assert actual == status, "Unexpected HTTP status"
        return SimpleNamespace(
            status_code=actual, content=content, json=lambda: {"code": code}
        )

    original_read = storage.read_verified

    def read(*args):
        with pytest.raises(MaintenanceBusy), storage_guard(root, exclusive=True):
            pytest.fail("Inventory released its production storage guard")
        reads.append(args)
        return original_read(*args)

    monkeypatch.setattr(storage, "read_verified", read)
    h.request = request
    return SimpleNamespace(
        h=h,
        replay=replay,
        control={"arms": [failed]},
        arms=arms,
        rows=rows,
        pins=pins,
        responses=responses,
        queries=queries,
        requests=requests,
        reads=reads,
        run=lambda: audit_helper()(h, replay, {"arms": [failed]}, arms),
    )


def test_actual_inventory_checks_every_byte_and_preserves_typed_denial(inventory):
    h = inventory
    before = h.run()
    assert h.run() == before
    assert len(h.reads) == len(h.rows) * 2
    assert all(args[3] == 50_000_000 for args in h.reads)
    assert all(query.startswith("SELECT ") for query in h.queries)
    denied = [entry for entry in before[2] if entry[1] == 409]
    assert len(denied) == 1 and denied[0][2] == "typed_final_result_required"
    assert len([entry for entry in h.requests if entry[1]]) == len(h.rows) * 2
    assert all(entry[2] == 404 for entry in h.requests if entry[1])
    failed = [row for row in h.rows if row[-1] == "failed"]
    assert len(failed) == 3
    assert all((row[0], False, 200) in h.requests for row in failed)


@pytest.mark.parametrize(
    "fault",
    [
        "unexpected_conflict",
        "wrong_code",
        "metrics_released",
        "foreign_owner",
        "http_hash",
        "http_size",
    ],
)
def test_actual_inventory_rejects_wrong_http_admission(inventory, fault):
    h = inventory
    metrics = next(row for row in h.rows if row[6].endswith("-metrics.json"))
    logs = next(row for row in h.rows if row[6].endswith("-logs.txt"))
    if fault == "unexpected_conflict":
        h.responses[logs[0]] = (409, b"", "typed_final_result_required")
    elif fault == "wrong_code":
        h.responses[metrics[0]] = (409, b"", "admission_required")
    elif fault == "metrics_released":
        h.responses[metrics[0]] = (200, b"metrics.json", None)
    elif fault == "foreign_owner":
        h.h.foreign_status = 200
    else:
        raw = h.responses[logs[0]][1]
        h.responses[logs[0]] = (
            200,
            b"x" * len(raw) if fault == "http_hash" else raw + b"x",
            None,
        )
    with pytest.raises(AssertionError):
        h.run()


@pytest.mark.parametrize(
    "fault", ["hash", "size", "symlink", "missing", "oversized", "pin"]
)
def test_actual_inventory_cannot_skip_fenced_physical_bytes(inventory, fault):
    h = inventory
    row = next(row for row in h.rows if row[6].endswith("-metrics.json"))
    path = h.h.root / "storage" / row[3]
    if fault == "hash":
        path.write_bytes(b"tampered")
    elif fault == "size":
        row[5] += 1
        next(pin for pin in h.pins if pin[1] == row[0])[3]["size_bytes"] = row[5]
    elif fault == "missing":
        path.unlink()
    elif fault == "oversized":
        # Keep registration and pin exact so the bounded read, specifically of
        # the denied sidecar, must reject it. All earlier files fit the limit.
        h.h.env["MAX_ARTIFACT_BYTES"] = "1024"
        raw = b"x" * 1025
        path.write_bytes(raw)
        row[4], row[5] = hashlib.sha256(raw).hexdigest(), len(raw)
        index = next(index for index, pin in enumerate(h.pins) if pin[1] == row[0])
        h.pins[index] = (
            h.pins[index][0],
            row[0],
            row[4],
            {"storage_key": row[3], "size_bytes": row[5]},
        )
    elif fault == "pin":
        next(pin for pin in h.pins if pin[1] == row[0])[3]["storage_key"] = "changed"
    else:
        target = h.h.root / "outside"
        path.rename(target)
        path.symlink_to(target)
    with pytest.raises((AssertionError, DomainError)):
        h.run()


@pytest.mark.parametrize(
    "column,value",
    [
        (1, uuid4()),
        (2, uuid4()),
        (6, "metrics.json"),
        (7, "tradegrad.run.v1"),
        (8, "optimization"),
        (9, uuid4()),
        (10, "c" * 64),
        (11, "failed"),
    ],
)
def test_metrics_denial_requires_exact_run_attempt_source_and_schema(
    inventory, column, value
):
    row = next(row for row in inventory.rows if row[6].endswith("-metrics.json"))
    row[column] = value
    with pytest.raises(AssertionError):
        inventory.run()


@pytest.mark.parametrize(
    "suffix", ["-metrics.json", "-result-bundle.json", "-logs.txt", "-provenance.json"]
)
def test_required_success_and_failed_diagnostics_cannot_disappear(inventory, suffix):
    inventory.rows[:] = [row for row in inventory.rows if not row[6].endswith(suffix)]
    inventory.pins[:] = [
        pin for pin in inventory.pins if any(row[0] == pin[1] for row in inventory.rows)
    ]
    with pytest.raises(AssertionError, match="lost required"):
        inventory.run()


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_production_publisher_and_loader_keep_legacy_metrics_fenced(
    tmp_path, monkeypatch, outcome
):
    config, _, _, _ = config_and_files()
    run = SimpleNamespace(
        id=uuid4(),
        owner_id=uuid4(),
        config=config.model_dump(mode="json"),
        research_image_digest=config.research_image_digest,
        status=outcome,
    )
    evaluation = SimpleNamespace(
        id=uuid4(),
        run_id=run.id,
        attempt_id=uuid4(),
        image_digest=config.research_image_digest,
        status=outcome,
    )
    publication = {
        "status": outcome,
        "execution_mode": "independent_evaluation",
        "image_digest": config.research_image_digest,
        "logs": {"output": "inert isolated execution diagnostic"},
    }
    if outcome == "succeeded":
        # This is an inert publisher input, not a valid or admitted economic result.
        publication["metrics"] = {"inert_fixture": True}
    else:
        publication["error"] = "deliberate isolated fixture failure"
    records = publish_artifacts(tmp_path, tmp_path / "artifacts", publication, 10000)
    assert {row["name"] for row in records} == (
        {"provenance.json", "logs.txt", "metrics.json"}
        if outcome == "succeeded"
        else {"provenance.json", "logs.txt"}
    )
    calls = []
    objects = {ResearchRun: run, Evaluation: evaluation}
    session = SimpleNamespace(get=lambda model, key: objects[model])
    monkeypatch.setattr(
        independent_services,
        "validate_independent_inputs",
        lambda *args, **kwargs: calls.append("source"),
    )
    monkeypatch.setattr(
        independent_services,
        "require_independent_admission",
        lambda *args: calls.append("admission"),
    )
    monkeypatch.setattr(
        result_access,
        "record_result_access",
        lambda *args, **kwargs: calls.append("exposure"),
    )
    settings = SimpleNamespace(storage_root=tmp_path, max_artifact_bytes=10000)
    opened = []
    original = storage.read_verified

    def read(*args):
        opened.append(args[1])
        return original(*args)

    monkeypatch.setattr(storage, "read_verified", read)
    for record in records:
        artifact = Artifact(
            id=uuid4(),
            run_id=run.id,
            attempt_id=evaluation.attempt_id,
            **{**record, "name": f"evaluation-{evaluation.id}-{record['name']}"},
        )
        calls.clear()
        opened.clear()
        with pytest.raises(DomainError) as wrong_owner:
            result_access.load_result_aware_artifact(
                session, artifact, uuid4(), settings
            )
        assert wrong_owner.value.status_code == 404
        assert not calls and not opened
        if record["name"] == "metrics.json":
            with pytest.raises(DomainError) as denied:
                result_access.load_result_aware_artifact(
                    session, artifact, run.owner_id, settings
                )
            assert (denied.value.status_code, denied.value.code) == (
                409,
                "typed_final_result_required",
            )
            assert not opened and calls == ["source", "admission"]
        else:
            raw = result_access.load_result_aware_artifact(
                session, artifact, run.owner_id, settings
            )
            assert hashlib.sha256(raw).hexdigest() == record["sha256"]
            assert len(raw) == record["size_bytes"]
            assert calls[:2] == ["source", "admission"]
            assert len(opened) == 1
            if record["name"] == "logs.txt":
                assert calls[-1] == "exposure"
            else:
                assert json.loads(raw)["execution_mode"] == "independent_evaluation"
