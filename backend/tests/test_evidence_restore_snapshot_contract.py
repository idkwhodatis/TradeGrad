"""Actual hosted helper and exposure writer/classifier with explicit inert doubles.

No PostgreSQL, socket, Docker, backup or strategy execution is claimed. The HTTP
and persistence doubles expose the real note-export metadata access side effect.
"""

import ast
import copy
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from backend.app import experiment_services, independent_services, research_note_routes
from backend.app.models import (
    DatasetVersion,
    DataUsageExposure,
    ExecutionAttempt,
    ResearchRun,
)
from backend.app.research_note_evidence import EvidenceResolver
from backend.app.result_access import record_result_access
from packages.tradegrad_adapter.config import DateWindow


def helpers(*, old_order=False):
    path = (
        Path(__file__).resolve().parents[2]
        / "tests/system/test_research_evidence_restore.py"
    )
    tree = ast.parse(path.read_text())
    names = {
        "_snapshot_http",
        "_trace_params",
        "_exact_note",
        "_snapshot_difference",
        "_assert_snapshot_equal",
    }
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if old_order:
        function = next(node for node in nodes if node.name == "_snapshot_http")
        index = next(
            i
            for i, node in enumerate(function.body)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "values"
                for target in node.targets
            )
        )
        # Reproduce the previous ordering only: controlled classification occupied
        # index four, before later note exports. Every other request is unchanged.
        function.body[index:index] = ast.parse(
            "requests.insert(4, requests.pop())"
        ).body

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    namespace = {
        "require": require,
        "uuid4": uuid4,
        "TRACE_FIELDS": (
            "execution_attempt_id",
            "result_manifest_sha256",
            "trace_manifest_sha256",
        ),
    }
    exec(  # noqa: S102 - only the checked-in helper AST, never candidate source
        compile(
            ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
            str(path),
            "exec",
        ),
        namespace,
    )
    return namespace


@pytest.fixture
def snapshot_harness(monkeypatch):
    owner, other = SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())
    dataset = SimpleNamespace(
        id=uuid4(),
        owner_id=owner.id,
        market="CN",
        manifest={"listings": [{"symbol": "FIXTURE"}]},
        start_date=date(2024, 1, 1),
        end_date=date(2024, 5, 1),
    )
    fields = {column.name: None for column in ResearchRun.__table__.columns}
    fields.update(
        id=uuid4(),
        owner_id=owner.id,
        generation=1,
        status="failed",
        request_sha256="a" * 64,
        config={
            "schema_version": "tradegrad.independent-evaluation.v1",
            "execution_mode": "independent_evaluation",
        },
        dataset_version_id=dataset.id,
        experiment_id=uuid4(),
        split_version_id=uuid4(),
    )
    run = SimpleNamespace(**fields)
    attempt = SimpleNamespace(
        id=uuid4(),
        run_id=run.id,
        generation=1,
        status="failed",
        finished_at=datetime(2024, 1, 2, tzinfo=UTC),
        sandbox_stopped=True,
    )
    snapshot = {
        "run_schema_version": run.config["schema_version"],
        "run_config_sha256": run.request_sha256,
        "failed_attempt": {
            "run_id": str(run.id),
            "execution_attempt_id": str(attempt.id),
            "status": "failed",
            "generation": 1,
            "config_sha256": run.request_sha256,
        },
    }
    exposures, calls = [], []

    def scalar(query):
        entity = query.column_descriptions[0]["entity"]
        if entity is ResearchRun:
            return run
        if entity is ExecutionAttempt:
            return attempt
        assert entity is DataUsageExposure
        params = query.compile().params
        assert params["owner_id_1"] == owner.id
        return next(
            (row for row in exposures if row.source_key == params["source_key_1"]), None
        )

    session = SimpleNamespace(
        scalar=scalar,
        get=lambda model, key: (
            dataset if model is DatasetVersion and key == dataset.id else None
        ),
        add=lambda row: exposures.append(row),
        flush=lambda: None,
        commit=lambda: None,
    )
    monkeypatch.setattr(
        "backend.app.research_note_evidence.advisory_lock", lambda *args: None
    )
    monkeypatch.setattr(experiment_services, "advisory_lock", lambda *args: None)
    monkeypatch.setattr(
        independent_services, "owned_independent", lambda *args: (None, run)
    )
    monkeypatch.setattr(
        independent_services, "require_independent_admission", lambda *args: None
    )

    def metadata(purpose="result_preview"):
        EvidenceResolver(session, owner.id, None, purpose=purpose).admit_snapshot(
            snapshot
        )

    # Real note creation/read already admitted this failed execution as preview.
    metadata()
    # The earlier _artifacts pass has already downloaded its diagnostic logs.
    # That does not create the distinct research-note-metadata download key.
    record_result_access(
        session,
        run,
        owner.id,
        purpose="result_download",
        source="artifact",
        artifact=SimpleNamespace(id=uuid4(), run_id=run.id, sha256="f" * 64),
        conservative=True,
    )
    initial_keys = {row.source_key for row in exposures}

    def pin():
        return {
            "evaluation_id": str(uuid4()),
            "execution_attempt_id": str(uuid4()),
            "source_id": str(uuid4()),
            "code_sha256": "b" * 64,
            "result_manifest_sha256": "c" * 64,
            "trace_manifest_sha256": "d" * 64,
            "spec_revision": 1,
            "spec_sha256": "e" * 64,
        }

    source, replay, control = (
        {"pin": pin()},
        {"id": str(uuid4()), "result_pin": pin()},
        {"id": str(uuid4())},
    )
    arms = [
        {"run_id": str(uuid4()), "result": pin()},
        {"run_id": str(uuid4()), "result": pin()},
        {"run_id": str(run.id), "result": None},
    ]
    comparison, decision = (
        {"id": str(uuid4()), "manifest_sha256": "f" * 64},
        {"decision_id": "inert-decision"},
    )
    notes = [
        {
            "note_id": str(uuid4()),
            "revision": number,
            "revision_sha256": digit * 64,
            "content": {
                "evidence": [{"kind": "diagnostic", "report_sha256": "9" * 64}]
            },
        }
        for number, digit in ((1, "7"), (2, "8"))
    ]
    export_bytes = " ".join(
        [note["revision_sha256"] for note in notes]
        + [
            source["pin"]["result_manifest_sha256"],
            comparison["manifest_sha256"],
            "9" * 64,
        ]
    ).encode()

    def read_revision(
        session,
        note_id,
        owner_id,
        settings,
        *,
        revision,
        revision_sha256,
        purpose="result_preview",
    ):
        assert owner_id == owner.id
        metadata(purpose)
        return copy.deepcopy(
            next(note for note in notes if note["note_id"] == str(note_id))
        )

    monkeypatch.setattr(research_note_routes, "read_revision", read_revision)
    monkeypatch.setattr(
        research_note_routes, "markdown", lambda value: export_bytes.decode()
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=None)))

    def exposure_view():
        return experiment_services.classify_exposures(
            experiment_services.dataset_identities(dataset),
            DateWindow(start=date(2024, 4, 1), end=date(2024, 5, 1)),
            exposures,
        )

    def http(client, method, path, *, status=200, **kwargs):
        calls.append((client is other, method, path, status))
        if status != 200:
            assert (client is other and status == 404) or (
                client is owner and status == 409
            )
            return SimpleNamespace(status_code=status)
        assert client is owner and method == "GET"
        content, value = (
            b"unchanged inert download",
            {"status": "succeeded", "result_manifest_sha256": "c" * 64},
        )
        if path.startswith("/controlled-experiments/"):
            value = {"id": control["id"], "exposure_snapshot": exposure_view()}
        elif path == "/research-notes":
            metadata()
            value = {"count": 1}
        elif path.startswith("/research-notes/"):
            note = next(note for note in notes if note["note_id"] in path)
            arguments = (
                UUID(note["note_id"]),
                note["revision"],
                note["revision_sha256"],
                owner,
                session,
                request,
            )
            if "/export?" in path:
                # The real export route, not the HTTP double, chooses download.
                content = research_note_routes.export(*arguments).body
            else:
                value = research_note_routes.exact(*arguments)
        return SimpleNamespace(
            status_code=200, content=content, json=lambda: copy.deepcopy(value)
        )

    h = SimpleNamespace(clients=[owner, other], request=http)
    result = SimpleNamespace(
        arguments=(h, source, notes, replay, control, arms, comparison, decision),
        exposures=exposures,
        calls=calls,
        initial_keys=initial_keys,
        view=exposure_view,
    )

    def restore_exposure_store():
        # A separate in-memory persistence copy models restored rows only. This
        # is explicitly not pg_dump/pg_restore or process-boundary acceptance.
        nonlocal exposures
        exposures = copy.deepcopy(exposures)
        result.exposures = exposures
        return exposures

    result.restore_exposure_store = restore_exposure_store
    return result


def test_prior_snapshot_order_diverges_on_exact_failed_note_download_exposure(
    snapshot_harness,
):
    h = snapshot_harness
    functions = helpers(old_order=True)
    before = functions["_snapshot_http"](*h.arguments)
    h.restore_exposure_store()
    after = functions["_snapshot_http"](*h.arguments)
    first, second = (
        dict(before)["controlled-experiment"],
        dict(after)["controlled-experiment"],
    )
    early, late = first["exposure_snapshot"], second["exposure_snapshot"]
    assert late["matching_count"] == early["matching_count"] + 1
    added = [row for row in h.exposures if row.source_key not in h.initial_keys]
    assert len(added) == 1
    assert added[0].source_key.startswith("access:research-note-metadata:")
    assert added[0].source_key.endswith(":result_download:0")
    assert set(late["matching_exposure_ids"]) - set(early["matching_exposure_ids"]) == {
        str(added[0].id)
    }
    assert early["matching_purposes"] == late["matching_purposes"]
    assert {key: value for key, value in before if key != "controlled-experiment"} == {
        key: value for key, value in after if key != "controlled-experiment"
    }
    with pytest.raises(
        AssertionError,
        match="controlled-experiment.*matching_exposure_ids.*array-length",
    ):
        functions["_assert_snapshot_equal"](before, after)


def test_actual_snapshot_reads_live_exposure_last_without_dropping_evidence(
    snapshot_harness,
):
    h = snapshot_harness
    functions = helpers()
    before = functions["_snapshot_http"](*h.arguments)
    keys = {row.source_key for row in h.exposures}
    original_store = h.exposures
    restored_store = h.restore_exposure_store()
    assert restored_store is not original_store
    assert all(
        new is not old for old, new in zip(original_store, restored_store, strict=True)
    )
    assert [row.id for row in restored_store] == [row.id for row in original_store]
    after = functions["_snapshot_http"](*h.arguments)
    functions["_assert_snapshot_equal"](before, after)
    assert {row.source_key for row in h.exposures} == keys
    assert len(keys - h.initial_keys) == 1
    assert len(before) == 23 and before[-1][0] == "controlled-experiment"
    snapshot = dict(before)["controlled-experiment"]["exposure_snapshot"]
    current = h.view()
    del current["as_of"]
    assert snapshot == current
    assert len(snapshot["matching_exposure_ids"]) == snapshot["matching_count"] == 3
    assert snapshot["matching_purposes"] == ["result_download", "result_preview"]
    assert sum(other for other, _, _, _ in h.calls) == len(before) * 2
    # Two revisions and repeated exports remain covered, without duplicate rows.
    assert sum("/export?" in path and not other for other, _, path, _ in h.calls) == 8


@pytest.mark.parametrize(
    "field",
    [
        "matching_count",
        "matching_exposure_ids",
        "matching_purposes",
        "classification",
        "independent_unseen_holdout_established",
    ],
)
def test_live_exposure_content_stays_strictly_compared(field):
    functions = helpers()
    before = [("controlled-experiment", {"exposure_snapshot": {field: "original"}})]
    after = [("controlled-experiment", {"exposure_snapshot": {field: "changed"}})]
    with pytest.raises(AssertionError, match=field):
        functions["_assert_snapshot_equal"](before, after)


@pytest.mark.parametrize(
    "category",
    [
        "source-spec",
        "diagnostic-parity",
        "comparison-result",
        "source-decision",
        "baseline-result",
        "failed-control-run",
        "first-note",
        "first-note-export",
    ],
)
def test_diagnostics_never_relax_hash_monetary_identity_or_binary_equality(category):
    functions = helpers()
    original = (
        b"original sensitive export"
        if category.endswith("export")
        else {
            "result_manifest_sha256": "a" * 64,
            "metrics": {"private_account": 123.456},
            "execution_attempt_id": "original-private-id",
        }
    )
    changes = (
        [b"changed sensitive export"]
        if isinstance(original, bytes)
        else [
            {**original, "result_manifest_sha256": "b" * 64},
            {**original, "metrics": {"private_account": 123.457}},
            {**original, "execution_attempt_id": "changed-private-id"},
        ]
    )
    for changed in changes:
        with pytest.raises(AssertionError) as error:
            functions["_assert_snapshot_equal"](
                [(category, original)], [(category, changed)]
            )
        message = str(error.value)
        assert category in message
        for secret in (
            "private_account",
            "123.45",
            "private-id",
            "sensitive export",
            "a" * 64,
            "b" * 64,
        ):
            assert secret not in message


def test_diagnostics_are_bounded_and_suppress_unknown_keys_values_and_endpoint_labels():
    functions = helpers()
    secret = f"postgresql://{uuid4().hex}:{uuid4().hex}@{uuid4().hex}/{uuid4().hex}"
    old, new = {secret: {"rows": [secret]}}, {secret: {"rows": [secret + "changed"]}}
    for _ in range(20):
        old, new = {secret: old}, {secret: new}
    with pytest.raises(AssertionError) as error:
        functions["_assert_snapshot_equal"](
            [("first-note", old)] * 20, [("first-note", new)] * 20
        )
    message = str(error.value)
    assert secret not in message and "private" not in message
    assert message.count("nested-value") == 4 and len(message) < 800
    with pytest.raises(AssertionError, match="endpoint-identity") as error:
        functions["_assert_snapshot_equal"]([(secret, old)], [(secret, new)])
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    "before,after,expected",
    [
        ({"status": "ok"}, {"status": 1}, "type string/integer"),
        ({"status": "ok"}, {}, "status field-presence"),
        ({"unlisted-secret-key": "value"}, {}, "<field> field-presence"),
        ([1], [1, 2], "array-length"),
    ],
)
def test_diagnostics_identify_only_allowlisted_fields_and_fixed_types(
    before, after, expected
):
    functions = helpers()
    with pytest.raises(AssertionError) as error:
        functions["_assert_snapshot_equal"](
            [("diagnostic-run", before)], [("diagnostic-run", after)]
        )
    assert expected in str(error.value) and "unlisted-secret-key" not in str(
        error.value
    )


def test_only_controlled_live_observation_timestamp_is_exempt(snapshot_harness):
    functions = helpers()
    h = snapshot_harness.arguments[0]
    original_request = h.request
    stamps = iter(("2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z"))

    def request(client, method, path, **kwargs):
        response = original_request(client, method, path, **kwargs)
        if path.startswith("/strategy-versions/") and kwargs.get("status", 200) == 200:
            value = response.json()
            value["exposure_snapshot"] = {"as_of": next(stamps)}
            response.json = lambda: copy.deepcopy(value)
        return response

    h.request = request
    before = functions["_snapshot_http"](*snapshot_harness.arguments)
    after = functions["_snapshot_http"](*snapshot_harness.arguments)
    with pytest.raises(
        AssertionError, match=r"source-spec \$\.exposure_snapshot\.as_of"
    ):
        functions["_assert_snapshot_equal"](before, after)
