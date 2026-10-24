"""Pure, actual-byte retention preview checks; all writes stay in tmp_path.

No PostgreSQL process, network/socket, Docker, evaluator or signal operations.
Fake snapshots exercise reference validation, not PostgreSQL integration.
"""

import copy
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.app import operations_retention as retention
from backend.app.operations_backup import BackupError, BackupLimits
from script import retention_v1

OWNER = "11111111-1111-4111-8111-111111111111"
RUN = "22222222-2222-4222-8222-222222222222"
ATTEMPT = "33333333-3333-4333-8333-333333333333"
EVALUATION = "44444444-4444-4444-8444-444444444444"
RUN_ROOT = f"runs/{RUN}/{ATTEMPT}"
TEMP = f"{RUN_ROOT}/.execution-environment.json.{'b' * 32}.tmp"
LOG = f"{RUN_ROOT}/evaluations/{EVALUATION}/artifacts/engine.log"


def sha(content):
    return hashlib.sha256(content).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class Database:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    def source_snapshot(self, limits):
        self.calls += 1
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return copy.deepcopy(self.snapshot)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    dataset = f"datasets/{OWNER}/version"
    checkpoint = f"{RUN_ROOT}/checkpoints/one"
    dataset_manifest = {
        "files": {"prices.csv": sha(b"prices")},
        "artifact_files": {"raw.json": sha(b"raw")},
        "preview_files": {
            "listing": [{"path": "preview.json", "sha256": sha(b"preview")}]
        },
    }
    checkpoint_manifest = {
        "files": [{"path": "state.json", "sha256": sha(b"state"), "size_bytes": 5}]
    }
    files = {
        TEMP: b"unfinished",
        LOG: b"diagnostics",
        f"{RUN_ROOT}/artifacts/result.json": b"result",
        f"{RUN_ROOT}/input/strategy.py": b"# inert source",
        f"{RUN_ROOT}/unreferenced.json": b"orphan retained",
        f"{dataset}/manifest.json": canonical(dataset_manifest),
        f"{dataset}/prices.csv": b"prices",
        f"{dataset}/raw.json": b"raw",
        f"{dataset}/preview.json": b"preview",
        f"{checkpoint}/manifest.json": canonical(checkpoint_manifest),
        f"{checkpoint}/state.json": b"state",
        f"uploads/{OWNER}/original.csv": b"source",
        f"data-checkpoints/{OWNER}/import/source.json": b"raw-block",
        f"strategy-packages/{OWNER}/one/archive.zip": b"package",
    }
    for key, data in files.items():
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    references = [
        {"path": key, "sha256": sha(files[key]), "size_bytes": len(files[key])}
        for key in (
            LOG,
            f"{RUN_ROOT}/artifacts/result.json",
            f"{checkpoint}/manifest.json",
        )
    ]
    snapshot = {
        "schema_revision": "005_settings",
        "postgres_major": 18,
        "references": references,
        "datasets": [
            {
                "path": dataset,
                "sha256": sha(files[f"{dataset}/manifest.json"]),
                "manifest": dataset_manifest,
            }
        ],
        "checkpoints": [
            {
                "path": f"{checkpoint}/manifest.json",
                "sha256": sha(files[f"{checkpoint}/manifest.json"]),
            }
        ],
    }
    return root, files, snapshot


def preview(source, **changes):
    root, _, snapshot = source
    arguments = {
        "database": Database(snapshot),
        "storage_root": root,
        "quiesced": True,
        "now": datetime.now(UTC) + timedelta(days=31),
    }
    arguments.update(changes)
    return retention.preview_retention(**arguments)


def assert_unchanged(source):
    root, expected, _ = source
    assert {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    } == expected


def test_preview_retains_all_source_final_dataset_checkpoint_package_and_log_bytes(
    source,
):
    result = preview(source)
    assert result["format"] == retention.FORMAT
    assert result["status"] == "preview_only"
    assert result["dry_run"] is True
    assert result["apply_supported"] is result["log_cleanup_supported"] is False
    assert result["deleted_files"] == 0
    assert result["retained_files"] == len(source[1])
    assert result["aged_run_logs_retained"] == 1
    assert [item["path"] for item in result["temporary_review_candidates"]] == [TEMP]
    assert result["temporary_review_bytes"] == len(source[1][TEMP])
    assert result["verified_reference_files"] == 8
    assert_unchanged(source)


def test_preview_age_configurable_and_uses_recent_ctime_not_only_forged_old_mtime(
    source,
):
    path = source[0] / TEMP
    os.utime(path, (1, 1))
    assert preview(source, now=datetime.now(UTC))["temporary_review_candidates"] == []
    assert (
        preview(
            source, policy=retention.RetentionPolicy(temporary_days=32, log_days=32)
        )["temporary_review_candidates"]
        == []
    )
    assert (
        preview(source, policy=retention.RetentionPolicy(log_days=32))[
            "aged_run_logs_retained"
        ]
        == 0
    )
    assert preview(source)["temporary_review_candidates"]


@pytest.mark.parametrize("name", ["logs.txt", "engine.log", "policy.log"])
def test_configured_attempt_logs_are_counted_but_never_deleted(source, name):
    # Actual configured workers publish beneath runs/<run>/attempts/<attempt>.
    key = f"runs/{RUN}/attempts/{ATTEMPT}/evaluations/{EVALUATION}/artifacts/{name}"
    path = source[0] / key
    path.parent.mkdir(parents=True, exist_ok=True)
    content = b"inert configured attempt diagnostics, not a disposable temporary"
    path.write_bytes(content)
    source[1][key] = content
    result = preview(source)
    assert result["aged_run_logs_retained"] == 2
    assert [item["path"] for item in result["temporary_review_candidates"]] == [TEMP]
    assert not retention.TEMPORARIES.fullmatch(key)
    assert result["deleted_files"] == 0 and result["log_cleanup_supported"] is False
    assert_unchanged(source)


@pytest.mark.parametrize(
    "field,value",
    [
        ("temporary_days", 0),
        ("log_days", -1),
        ("temporary_days", 36501),
        ("log_days", True),
        ("log_days", 1.5),
    ],
)
def test_invalid_policy_rejected(field, value):
    with pytest.raises(retention.RetentionError):
        retention.RetentionPolicy(**{field: value})


def test_apply_refused_before_any_database_or_filesystem_access(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Apply must not perform any inspection or mutation")

    monkeypatch.setattr(retention, "_open_directory", forbidden)
    database = Database({})
    monkeypatch.setattr(database, "source_snapshot", forbidden)
    with pytest.raises(retention.RetentionError, match="exclusion protocol"):
        retention.preview_retention(
            database=database, storage_root=Path("/never-read"), apply=True
        )


def test_acknowledgement_and_timezone_required(source):
    with pytest.raises(retention.RetentionError, match="every writer"):
        preview(source, quiesced=False)
    with pytest.raises(retention.RetentionError, match="timezone"):
        preview(source, now=datetime(2026, 1, 1))


def test_active_work_inspection_failure_is_closed_and_sanitized(source):
    database = Database(BackupError("Active work; private detail must not escape"))
    with pytest.raises(
        retention.RetentionError, match="Cold inspection failed"
    ) as error:
        preview(source, database=database)
    assert "private" not in str(error.value)
    assert_unchanged(source)


def test_referenced_temporary_is_never_a_candidate(source):
    source[2]["references"].append({"path": TEMP, "sha256": sha(source[1][TEMP])})
    assert preview(source)["temporary_review_candidates"] == []


def test_unreferenced_logs_and_unknown_temporary_layouts_are_retained(source):
    source[2]["references"] = [
        item for item in source[2]["references"] if item["path"] != LOG
    ]
    for key in (
        f"{RUN_ROOT}/arbitrary.tmp",
        f"{RUN_ROOT}/.source.py.{'b' * 32}.tmp",
        "uploads/unknown/.x.tmp",
    ):
        path = source[0] / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"unknown")
        source[1][key] = b"unknown"
    result = preview(source)
    assert [item["path"] for item in result["temporary_review_candidates"]] == [TEMP]
    assert result["aged_run_logs_retained"] == 1
    assert_unchanged(source)


@pytest.mark.parametrize(
    "kind",
    [
        "file_symlink",
        "directory_symlink",
        "root_symlink",
        "ancestor_symlink",
        "hardlink",
        "fifo",
    ],
)
def test_unsafe_filesystem_is_rejected_without_following_links(source, tmp_path, kind):
    root = source[0]
    if kind == "file_symlink":
        (root / "uploads" / "link").symlink_to(root / TEMP)
    elif kind == "directory_symlink":
        (root / "uploads" / "link").symlink_to(root / "runs", target_is_directory=True)
    elif kind == "root_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    elif kind == "ancestor_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        root = alias / "storage"
    elif kind == "hardlink":
        os.link(root / TEMP, root / "uploads" / "link")
    else:
        os.mkfifo(root / "uploads" / "fifo")
    with pytest.raises(retention.RetentionError):
        preview(source, storage_root=root)


def test_parent_traversal_and_unknown_namespace_rejected(source):
    with pytest.raises(retention.RetentionError):
        preview(source, storage_root=source[0] / ".." / "storage")
    (source[0] / "secrets").mkdir()
    with pytest.raises(retention.RetentionError):
        preview(source)


@pytest.mark.parametrize(
    "limit,value",
    [
        ("max_files", 1),
        ("max_file_bytes", 2),
        ("max_total_bytes", 5),
        ("max_manifest_bytes", 1),
    ],
)
def test_inventory_and_manifest_limits_fail_closed(source, limit, value):
    with pytest.raises(retention.RetentionError):
        preview(source, limits=BackupLimits(**{limit: value}))


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "hash",
        "size",
        "unknown_namespace",
        "unknown_revision",
        "unknown_snapshot",
        "dataset_manifest",
        "checkpoint_shape",
    ],
)
def test_unknown_or_invalid_references_fail_closed(source, mutation):
    root, _files, snapshot = source
    if mutation == "missing":
        (root / LOG).unlink()
    elif mutation == "hash":
        (root / LOG).write_bytes(b"corruption")
    elif mutation == "size":
        snapshot["references"][0]["size_bytes"] += 1
    elif mutation == "unknown_namespace":
        snapshot["references"][0]["path"] = "runner-state/x"
    elif mutation == "unknown_revision":
        snapshot["schema_revision"] = "006_unknown"
    elif mutation == "unknown_snapshot":
        snapshot["unknown_references"] = []
    elif mutation == "dataset_manifest":
        snapshot["datasets"][0]["manifest"] = {"files": {}}
    else:
        checkpoint = snapshot["checkpoints"][0]
        content = canonical(
            {
                "files": [
                    {"path": "state.json", "sha256": sha(b"state"), "size_bytes": True}
                ]
            }
        )
        (root / checkpoint["path"]).write_bytes(content)
        checkpoint["sha256"] = sha(content)
        snapshot["references"] = [
            item
            for item in snapshot["references"]
            if item["path"] != checkpoint["path"]
        ]
    with pytest.raises(retention.RetentionError):
        preview(source)


def test_database_reference_change_between_scans_is_rejected(source):
    class Changing(Database):
        def source_snapshot(self, limits):
            value = super().source_snapshot(limits)
            if self.calls > 1:
                value["references"] = []
            return value

    with pytest.raises(retention.RetentionError, match="Database references changed"):
        preview(source, database=Changing(source[2]))


def test_file_replaced_between_scans_is_rejected_even_with_same_bytes(
    source, monkeypatch
):
    original = retention._scan
    calls = 0

    def scan(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == 1:
            path = source[0] / TEMP
            path.unlink()
            path.write_bytes(source[1][TEMP])
        return result

    monkeypatch.setattr(retention, "_scan", scan)
    with pytest.raises(retention.RetentionError, match="Storage changed"):
        preview(source)


def test_file_changes_during_descriptor_read_fail_closed(source, monkeypatch):
    original = retention.os.read
    changed = False

    def read(fd, maximum):
        nonlocal changed
        data = original(fd, maximum)
        if data == b"unfinished" and not changed:
            changed = True
            (source[0] / TEMP).write_bytes(b"changed")
        return data

    monkeypatch.setattr(retention.os, "read", read)
    with pytest.raises(retention.RetentionError, match="Storage changed"):
        preview(source)


def test_cli_default_dry_run_and_apply_exit_code(source, monkeypatch, capsys):
    monkeypatch.setenv("RETENTION_TEST_URL", "private-connection-value")
    monkeypatch.setattr(
        retention_v1, "PostgresDatabase", lambda value: Database(source[2])
    )
    # The old advisory helper is still tested here; actual locked apply is a
    # separate real-PG gate, not silently simulated by this CLI-contract test.
    monkeypatch.setattr(
        retention_v1,
        "cold_retention",
        lambda **kwargs: {"dry_run": not kwargs["apply"]},
    )
    args = [
        "--storage",
        str(source[0]),
        "--database-url-env",
        "RETENTION_TEST_URL",
        "--acknowledge-quiesced",
    ]
    assert retention_v1.main(args) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["dry_run"] is True
    assert "private-connection-value" not in output.out + output.err

    def forbidden(value):
        pytest.fail("--apply must not construct a database connection")

    monkeypatch.setattr(retention_v1, "PostgresDatabase", forbidden)
    assert retention_v1.main(args + ["--apply"]) == 2
    assert "requires" in capsys.readouterr().err
    assert_unchanged(source)


def test_cli_failure_never_prints_connection_or_path(source, monkeypatch, capsys):
    monkeypatch.setenv("RETENTION_TEST_URL", "private-connection-value")

    def fail(value):
        raise RuntimeError("private-connection-value /private/path")

    monkeypatch.setattr(retention_v1, "PostgresDatabase", fail)
    assert (
        retention_v1.main(
            [
                "--storage",
                str(source[0]),
                "--database-url-env",
                "RETENTION_TEST_URL",
                "--acknowledge-quiesced",
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert "private-connection-value" not in output.out + output.err
    assert "/private/path" not in output.out + output.err


@pytest.mark.parametrize(
    "suffix",
    [
        "prices.csv",
        "raw.json",
        "preview.json",
        "state.json",
        "manifest.json",
        "result.json",
    ],
)
def test_each_referenced_file_class_is_integrity_checked(source, suffix):
    key = next(key for key in source[1] if key.endswith("/" + suffix))
    (source[0] / key).write_bytes(b"corrupt")
    with pytest.raises(retention.RetentionError):
        preview(source)


def test_duplicate_checkpoint_manifest_fields_rejected(source):
    checkpoint = source[2]["checkpoints"][0]
    content = b'{"files": [], "files": []}'
    (source[0] / checkpoint["path"]).write_bytes(content)
    checkpoint["sha256"] = sha(content)
    source[2]["references"] = [
        item for item in source[2]["references"] if item["path"] != checkpoint["path"]
    ]
    with pytest.raises(retention.RetentionError, match="manifest"):
        preview(source)


def test_deep_storage_tree_rejected(source):
    deep = source[0] / "runs"
    for _ in range(33):
        deep /= "nested"
    deep.mkdir(parents=True)
    with pytest.raises(retention.RetentionError, match="depth"):
        preview(source)


def test_scan_timeout_is_not_a_partial_report(source, monkeypatch):
    tick = -1000

    def clock():
        nonlocal tick
        tick += 1000
        return tick

    monkeypatch.setattr(retention.time, "monotonic", clock)
    with pytest.raises(retention.RetentionError, match="time budget"):
        preview(source, limits=BackupLimits(timeout_seconds=1))
    assert_unchanged(source)


@pytest.mark.parametrize(
    "key",
    [
        f"uploads/{OWNER}/.{'a' * 64}.csv.{'b' * 32}.tmp",
        f"data-checkpoints/{OWNER}/{ATTEMPT}/.{'a' * 64}.json.{'b' * 32}.tmp",
        f"strategy-packages/{OWNER}/{ATTEMPT}/.{'a' * 64}.zip.{'b' * 32}.tmp",
        f"{RUN_ROOT}/evaluations/{EVALUATION}/artifacts/.policy.log.{'b' * 32}.tmp",
    ],
)
def test_confirmed_atomic_publisher_layouts_are_advisory_only(source, key):
    path = source[0] / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"temporary")
    source[1][key] = b"temporary"
    result = preview(source)
    assert key in {item["path"] for item in result["temporary_review_candidates"]}
    assert result["deleted_files"] == 0
    assert_unchanged(source)


def test_root_replacement_between_scans_is_rejected(source, monkeypatch):
    original = retention._scan
    calls = 0

    def scan(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == 1:
            root = source[0]
            root.rename(root.with_name("old-storage"))
            root.mkdir()
            for key, data in source[1].items():
                path = root / key
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        return result

    monkeypatch.setattr(retention, "_scan", scan)
    with pytest.raises(retention.RetentionError, match="Storage changed"):
        preview(source)


@pytest.mark.parametrize("revision", ["010_research_notes", "011_note_pin_trigger"])
def test_historical_note_pin_is_protected_without_current_artifact_row(
    source, revision
):
    """Data-only snapshot seam: a retained old revision remains an authority.

    The synthetic temporary-shaped key tests the deletion exclusion itself;
    normal published note evidence was already unconditionally retained.
    """
    _root, files, snapshot = source
    snapshot["schema_revision"] = revision
    snapshot["references"].append(
        {"path": TEMP, "sha256": sha(files[TEMP]), "size_bytes": len(files[TEMP])}
    )
    result = preview(source)
    assert result["temporary_review_candidates"] == []
    assert result["deleted_files"] == 0
    assert_unchanged(source)


@pytest.mark.parametrize("revision", ["010_research_notes", "011_note_pin_trigger"])
def test_corrupt_historical_note_reference_stops_retention_not_retargeted(
    source, revision
):
    _root, files, snapshot = source
    snapshot["schema_revision"] = revision
    snapshot["references"].append(
        {
            "path": TEMP,
            "sha256": sha(b"different-original"),
            "size_bytes": len(files[TEMP]),
        }
    )
    with pytest.raises(retention.RetentionError, match="Reference bytes"):
        preview(source)
    assert_unchanged(source)
