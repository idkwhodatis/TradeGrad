"""Actual-byte cold-backup tests; no PostgreSQL, network or real client process."""

import copy
import hashlib
import io
import json
import os
import secrets

import pytest

from backend.app import operations_backup as backup
from script import backup_v1

APP_REF = "a" * 40


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class FakeDatabase:
    database_name = "tradegrad_empty_test"

    def __init__(self, snapshot, *, empty=True, major=18):
        self.snapshot = copy.deepcopy(snapshot)
        self.empty = empty
        self.major = major
        self.inspections = 0

    def source_snapshot(self, limits):
        self.inspections += 1
        if isinstance(self.snapshot, BaseException):
            raise self.snapshot
        return copy.deepcopy(self.snapshot)

    def empty_target_major(self):
        if not self.empty:
            raise backup.BackupError("Restore requires an EMPTY database")
        return self.major

    def environment(self):
        return {
            "PGPASSWORD": secrets.token_urlsafe(32),
            "PGDATABASE": self.database_name,
        }


class FakeClients:
    """Protocol fake, not SQL export/reconstruction; bytes exercise the filesystem."""

    def __init__(self, *, failure=None, major=18):
        self.calls = []
        self.failure = failure
        self.major = major
        self.archive = b"PGDMP\x01\x02\x03immutable-fake-client-archive"
        self.restored_bytes = None

    def __call__(self, command, *, env, stdin, stdout, timeout, max_output_bytes):
        self.calls.append(command)
        contains_credential = env["PGPASSWORD"] in " ".join(command)
        assert not contains_credential
        assert "postgresql://" not in " ".join(command)
        assert timeout > 0 and max_output_bytes > 0
        if command[-1] == "--version":
            stdout.write(f"{command[0]} (PostgreSQL) {self.major}.6\n".encode())
            return
        assert env["PGDATABASE"] == "tradegrad_empty_test"
        if command[0] == "pg_dump":
            assert stdin is None
            assert "--format=custom" in command
            assert not any("--file" in arg for arg in command)
            stdout.write(self.archive)
        elif command[0] == "pg_restore":
            assert "--single-transaction" in command
            assert "--exit-on-error" in command
            assert not {"--clean", "--create"} & set(command)
            assert command[-1] == "--dbname=tradegrad_empty_test"
            self.restored_bytes = stdin.read()
        else:
            raise AssertionError("Unexpected client")
        if self.failure == command[0]:
            raise backup.BackupError("Injected PostgreSQL failure")
        if self.failure == "interrupt":
            raise KeyboardInterrupt

    @property
    def tools(self):
        return backup.PgTools(runner=self)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir(mode=0o700)
    data = b"date,close\n2024-01-02,12.3\n"
    parquet = b"PAR1private-market-data\x00\x01"
    preview = b'[{"close":12.3}]'
    checkpoint_data = b'{"candidate":"immutable","rng":[1,2,3]}'
    checkpoint = {
        "files": [
            {
                "path": "state.json",
                "sha256": digest(checkpoint_data),
                "size_bytes": len(checkpoint_data),
            }
        ]
    }
    manifest = {
        "files": {"data.csv": digest(data)},
        "artifact_files": {"data.parquet": digest(parquet)},
        "preview_files": {
            "listing": [{"path": "preview.json", "sha256": digest(preview)}]
        },
    }
    content = {
        "datasets/owner/version/manifest.json": canonical(manifest),
        "datasets/owner/version/data.csv": data,
        "datasets/owner/version/data.parquet": parquet,
        "datasets/owner/version/preview.json": preview,
        "runs/run/attempt/results.json": b'{"result":"immutable"}',
        "runs/run/checkpoint/manifest.json": canonical(checkpoint),
        "runs/run/checkpoint/state.json": checkpoint_data,
        "strategy-packages/owner/package/content.zip": b"PK\x03\x04private-export",
        "uploads/owner/source.csv": b"private-upload",
        "data-checkpoints/owner/import/block.json": b'[{"price":123}]',
    }
    for key, value in content.items():
        path = root / key
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(value)
        path.chmod(0o400)
    (root / "datasets/empty").mkdir()
    references = [
        {"path": key, "sha256": digest(content[key]), "size_bytes": len(content[key])}
        for key in (
            "runs/run/attempt/results.json",
            "runs/run/checkpoint/manifest.json",
            "strategy-packages/owner/package/content.zip",
            "data-checkpoints/owner/import/block.json",
            "uploads/owner/source.csv",
        )
    ]
    snapshot = {
        "schema_revision": "004_m5",
        "postgres_major": 18,
        "references": references,
        "datasets": [
            {
                "path": "datasets/owner/version",
                "sha256": digest(canonical(manifest)),
                "manifest": manifest,
            }
        ],
        "checkpoints": [
            {
                "path": "runs/run/checkpoint/manifest.json",
                "sha256": digest(canonical(checkpoint)),
            }
        ],
    }
    return root, content, snapshot


def create(tmp_path, source, *, clients=None, **kwargs):
    root, _, snapshot = source
    clients = clients or FakeClients()
    values = {
        "database": FakeDatabase(snapshot),
        "storage_root": root,
        "destination": tmp_path / "backup",
        "durable_parent": tmp_path,
        "application_ref": APP_REF,
        "quiesced": True,
        "tools": clients.tools,
    }
    values.update(kwargs)
    return backup.create_backup(**values)


def restore(tmp_path, source, *, clients=None, **kwargs):
    clients = clients or FakeClients()
    values = {
        "database": FakeDatabase(source[2]),
        "backup_root": tmp_path / "backup",
        "storage_root": tmp_path / "restored",
        "durable_parent": tmp_path,
        "application_ref": APP_REF,
        "quiesced": True,
        "trust_backup": True,
        "tools": clients.tools,
    }
    values.update(kwargs)
    return backup.restore_backup(**values)


def rewrite_manifest(root, mutate):
    value = json.loads((root / "manifest.json").read_bytes())
    mutate(value)
    encoded = canonical(value)
    (root / "manifest.json").write_bytes(encoded)
    (root / "COMPLETE").write_bytes(digest(encoded).encode() + b"\n")


def test_complete_backup_restore_preserves_every_immutable_byte_and_private_modes(
    tmp_path, source
):
    clients = FakeClients()
    manifest = create(tmp_path, source, clients=clients)
    assert manifest["schema_revision"] == "004_m5"
    assert manifest["application_ref"] == APP_REF
    assert (
        backup.verify_backup(tmp_path / "backup", application_ref=APP_REF) == manifest
    )
    assert not any(
        "password" in key.lower() or "url" in key.lower() for key in manifest
    )
    assert restore(tmp_path, source, clients=clients) == manifest
    assert clients.restored_bytes == clients.archive
    assert not (tmp_path / "restored/.restore-incomplete").exists()
    for key, value in source[1].items():
        target = tmp_path / "restored" / key
        assert target.read_bytes() == value
        assert target.stat().st_mode & 0o377 == 0
    assert (tmp_path / "restored/datasets/empty").is_dir()
    for root in (tmp_path / "backup", tmp_path / "restored"):
        assert root.stat().st_mode & 0o077 == 0
        for path in root.rglob("*"):
            assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("quiesced", [False, None, "yes", 1])
def test_creation_requires_explicit_quiescence_before_database_or_file_work(
    tmp_path, source, quiesced
):
    database = FakeDatabase(AssertionError("must not inspect"))
    with pytest.raises(backup.BackupError, match="acknowledgement"):
        create(tmp_path, source, quiesced=quiesced, database=database)
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("kwargs", [{"quiesced": False}, {"trust_backup": False}])
def test_restore_requires_both_stopped_writers_and_trusted_sql(
    tmp_path, source, kwargs
):
    with pytest.raises(backup.BackupError, match="explicit"):
        restore(tmp_path, source, **kwargs)
    assert not (tmp_path / "restored").exists()


def test_active_database_failures_stop_backup_before_destination_creation(
    tmp_path, source
):
    with pytest.raises(backup.BackupError, match="Active"):
        create(
            tmp_path,
            source,
            database=FakeDatabase(backup.BackupError("Active execution")),
        )
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("name", ["backup", "restored"])
def test_existing_empty_or_partial_destination_is_never_reused(tmp_path, source, name):
    if name == "restored":
        create(tmp_path, source)
    target = tmp_path / name
    target.mkdir()
    (target / "preserve-me").write_bytes(b"old")
    with pytest.raises(backup.BackupError, match="already exists"):
        (create if name == "backup" else restore)(tmp_path, source)
    assert (target / "preserve-me").read_bytes() == b"old"


def test_existing_target_database_never_receives_sql_or_new_storage(tmp_path, source):
    create(tmp_path, source)
    clients = FakeClients()
    with pytest.raises(backup.BackupError, match="EMPTY"):
        restore(
            tmp_path,
            source,
            clients=clients,
            database=FakeDatabase(source[2], empty=False),
        )
    assert not clients.calls
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("failure", ["pg_dump", "interrupt"])
def test_dump_failure_or_interrupt_leaves_no_complete_backup(tmp_path, source, failure):
    clients = FakeClients(failure=failure)
    with pytest.raises((backup.BackupError, KeyboardInterrupt)):
        create(tmp_path, source, clients=clients)
    assert not (tmp_path / "backup/COMPLETE").exists()
    with pytest.raises((backup.BackupError, OSError)):
        backup.verify_backup(tmp_path / "backup")
    with pytest.raises(backup.BackupError, match="already exists"):
        create(tmp_path, source)


def test_interrupted_copy_leaves_source_unchanged_and_no_complete_backup(
    tmp_path, source, monkeypatch
):
    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(backup, "_copy", interrupt)
    with pytest.raises(KeyboardInterrupt):
        create(tmp_path, source)
    assert not (tmp_path / "backup/COMPLETE").exists()
    for key, value in source[1].items():
        assert (source[0] / key).read_bytes() == value


def test_restore_failure_keeps_explicit_incomplete_marker_and_refuses_retry(
    tmp_path, source
):
    create(tmp_path, source)
    with pytest.raises(backup.BackupError, match="Injected"):
        restore(tmp_path, source, clients=FakeClients(failure="pg_restore"))
    assert (tmp_path / "restored/.restore-incomplete").is_file()
    with pytest.raises(backup.BackupError, match="already exists"):
        restore(tmp_path, source)


@pytest.mark.parametrize(
    "file",
    [
        "database.dump",
        "storage/datasets/owner/version/data.parquet",
        "manifest.json",
        "COMPLETE",
    ],
)
def test_tamper_detected_before_any_restore_action(tmp_path, source, file):
    create(tmp_path, source)
    path = tmp_path / "backup" / file
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"tampered")
    clients = FakeClients()
    with pytest.raises((backup.BackupError, OSError)):
        restore(tmp_path, source, clients=clients)
    assert not clients.calls
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("application_ref", "b" * 40),
        ("schema_revision", "999_unknown"),
        ("format", "future.v2"),
        ("postgres_major", True),
    ],
)
def test_manifest_versions_are_fail_closed(tmp_path, source, field, value):
    create(tmp_path, source)
    rewrite_manifest(
        tmp_path / "backup", lambda manifest: manifest.update({field: value})
    )
    with pytest.raises(backup.BackupError):
        restore(tmp_path, source)
    assert not (tmp_path / "restored").exists()


def test_server_and_client_major_must_match_recorded_version(tmp_path, source):
    with pytest.raises(backup.BackupError, match="version mismatch"):
        create(tmp_path, source, clients=FakeClients(major=17))
    create(tmp_path, source)
    with pytest.raises(backup.BackupError, match="major version"):
        restore(tmp_path, source, database=FakeDatabase(source[2], major=17))


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/absolute",
        "storage/../secret",
        "storage/runs//file",
        "storage/runs/./file",
        "storage/runs/back\\slash",
        "storage/credentials/password",
        "storage/runs/a\x00b",
    ],
)
def test_untrusted_manifest_paths_rejected_even_with_recomputed_integrity_marker(
    tmp_path, source, path
):
    create(tmp_path, source)
    rewrite_manifest(
        tmp_path / "backup", lambda manifest: manifest["files"][1].update(path=path)
    )
    with pytest.raises(backup.BackupError):
        restore(tmp_path, source)
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "kind", ["symlink-file", "symlink-dir", "hardlink", "fifo", "runner", "credentials"]
)
def test_unsafe_source_entries_rejected(tmp_path, source, kind):
    root = source[0]
    if kind == "symlink-file":
        (root / "runs/link").symlink_to(root / "uploads/owner/source.csv")
    elif kind == "symlink-dir":
        (root / "runs/link").symlink_to(root / "uploads", target_is_directory=True)
    elif kind == "hardlink":
        os.link(root / "uploads/owner/source.csv", root / "runs/link")
    elif kind == "fifo":
        os.mkfifo(root / "runs/pipe")
    else:
        (root / kind).mkdir()
    with pytest.raises(backup.BackupError):
        create(tmp_path, source)
    assert not (tmp_path / "backup").exists()


def test_symlink_ancestor_and_outside_durable_parent_rejected(tmp_path, source):
    alias = tmp_path / "alias"
    alias.symlink_to(source[0], target_is_directory=True)
    with pytest.raises(backup.BackupError):
        create(tmp_path, source, storage_root=alias)
    with pytest.raises(backup.BackupError, match="durable parent"):
        create(tmp_path, source, durable_parent=source[0])


@pytest.mark.parametrize(
    "limit",
    [
        "max_files",
        "max_file_bytes",
        "max_total_bytes",
        "max_dump_bytes",
        "max_manifest_bytes",
    ],
)
def test_all_inventory_and_byte_limits_enforced(tmp_path, source, limit):
    with pytest.raises(backup.BackupError):
        create(tmp_path, source, limits=backup.BackupLimits(**{limit: 1}))
    assert not (tmp_path / "backup/COMPLETE").exists()


def test_missing_immutable_dataset_or_checkpoint_bytes_prevent_backup(tmp_path, source):
    (source[0] / "datasets/owner/version/data.parquet").unlink()
    with pytest.raises(backup.BackupError, match="Database reference"):
        create(tmp_path, source)
    assert not (tmp_path / "backup").exists()


def test_changed_checkpoint_hash_rejects_source_even_when_file_exists(tmp_path, source):
    path = source[0] / "runs/run/checkpoint/state.json"
    path.chmod(0o600)
    path.write_bytes(b"tampered")
    with pytest.raises(backup.BackupError, match="Database reference"):
        create(tmp_path, source)


def test_database_change_between_dump_and_completion_is_rejected(tmp_path, source):
    database = FakeDatabase(source[2])
    original = database.source_snapshot

    def snapshot(limits):
        result = original(limits)
        if database.inspections > 1:
            result["schema_revision"] = "003_m3"
        return result

    database.source_snapshot = snapshot
    with pytest.raises(backup.BackupError, match="Source changed"):
        create(tmp_path, source, database=database)
    assert not (tmp_path / "backup/COMPLETE").exists()


def test_restore_revalidates_database_references_and_leaves_incomplete_on_mismatch(
    tmp_path, source
):
    create(tmp_path, source)
    database = FakeDatabase(source[2])
    database.snapshot["references"][0]["sha256"] = "0" * 64
    with pytest.raises(backup.BackupError, match="Database reference"):
        restore(tmp_path, source, database=database)
    assert (tmp_path / "restored/.restore-incomplete").exists()


def test_unlisted_backup_file_or_directory_is_rejected(tmp_path, source):
    create(tmp_path, source)
    extra = tmp_path / "backup/storage/runs/unlisted"
    extra.mkdir()
    with pytest.raises(backup.BackupError, match="inventory differs"):
        backup.verify_backup(tmp_path / "backup")
    extra.rmdir()
    extra.write_bytes(b"unlisted")
    with pytest.raises(backup.BackupError, match="inventory differs"):
        backup.verify_backup(tmp_path / "backup")


def test_database_credentials_only_in_private_client_env_and_no_ambient_pg_settings(
    monkeypatch,
):
    credential = secrets.token_urlsafe(32)
    monkeypatch.setenv("API_KEY", secrets.token_urlsafe(32))
    database = backup.PostgresDatabase(
        f"postgresql+psycopg://operator:{credential}@db.example:5432/tradegrad?sslmode=require"
    )
    env = database.environment()
    credential_matches = secrets.compare_digest(env["PGPASSWORD"], credential)
    assert credential_matches
    assert env["PGDATABASE"] == "tradegrad"
    assert env["PGSSLMODE"] == "require"
    assert env["PGPASSFILE"] == "/dev/null"
    assert "API_KEY" not in env and "PGSERVICE" not in env
    contains_credential = credential in repr(database)
    assert not contains_credential


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///x",
        "postgresql://host/db?options=anything",
        "postgresql://host/a%3Db",
        "postgresql://host/db?sslmode=invalid",
    ],
)
def test_unsupported_connections_fail_without_echoing_url(url):
    with pytest.raises(backup.BackupError) as error:
        backup.PostgresDatabase(url)
    assert url not in str(error.value)


def test_cli_never_logs_connection_exception_password_url_or_dump(
    tmp_path, source, monkeypatch, capsys
):
    credential = secrets.token_urlsafe(32)
    secret = f"postgresql://user:{credential}@example/db"
    monkeypatch.setenv("DATABASE_URL", secret)

    def fail(**kwargs):
        raise RuntimeError(secret + " PGDMP-private-application-data")

    monkeypatch.setattr(backup_v1, "create_backup", fail)
    result = backup_v1.main(
        [
            "create",
            "--backup",
            str(tmp_path / "backup"),
            "--storage",
            str(source[0]),
            "--durable-parent",
            str(tmp_path),
            "--application-ref",
            APP_REF,
            "--acknowledge-quiesced",
        ]
    )
    assert result == 1
    output = capsys.readouterr()
    contains_credential = credential in output.err or credential in output.out
    assert not contains_credential
    assert "postgresql" not in output.err and "PGDMP" not in output.err


def test_subprocess_launch_failure_redacts_os_error(monkeypatch):
    credential = secrets.token_urlsafe(32)

    def fail(*args, **kwargs):
        raise OSError(f"postgresql://operator:{credential}@host/db")

    monkeypatch.setattr(backup.subprocess, "Popen", fail)
    with pytest.raises(backup.BackupError) as error:
        backup.bounded_subprocess(
            ("pg_dump",),
            env={},
            stdin=None,
            stdout=io.BytesIO(),
            timeout=1,
            max_output_bytes=20,
        )
    contains_credential = credential in str(error.value)
    assert not contains_credential


class QueryResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value

    def mappings(self):
        return self.value


class InspectionConnection:
    def __init__(self, *, blocking=None, revisions=None):
        self.blocking = blocking
        self.revisions = ["004_m5"] if revisions is None else revisions
        self.queries = []

    def execute(self, statement, parameters=None):
        query = str(statement)
        self.queries.append(query)
        if query == "SHOW server_version_num":
            return QueryResult("180006")
        if "SELECT version_num" in query:
            return QueryResult(self.revisions)
        if self.blocking and self.blocking in query:
            return QueryResult(1)
        if "count(*)" in query:
            return QueryResult(0)
        assert parameters["limit"] > 0
        return QueryResult([])


@pytest.mark.parametrize(
    "blocking",
    [
        "FROM research_runs WHERE",
        "FROM execution_attempts WHERE",
        "FROM research_evaluations WHERE",
        "FROM data_imports WHERE",
        "FROM procrastinate_jobs WHERE",
    ],
)
def test_actual_source_inspector_checks_each_execution_and_import_fence(
    monkeypatch, blocking
):
    database = backup.PostgresDatabase(
        "postgresql://operator@localhost/unit_inspection"
    )
    connection = InspectionConnection(blocking=blocking)
    monkeypatch.setattr(database, "_inspect", lambda inspect: inspect(connection))
    with pytest.raises(backup.BackupError, match="Active, queued, leased or unstopped"):
        database.source_snapshot(backup.BackupLimits())
    assert any(blocking in query for query in connection.queries)


@pytest.mark.parametrize(
    "blocking",
    [
        "FROM pg_namespace",
        "FROM pg_largeobject_metadata",
        "FROM pg_extension",
        "FROM pg_foreign_server",
        "FROM pg_foreign_data_wrapper",
        "FROM pg_event_trigger",
        "FROM pg_publication",
        "FROM pg_subscription",
        "FROM pg_class",
        "FROM pg_collation",
        "FROM pg_conversion",
        "FROM pg_operator",
        "FROM pg_opclass",
        "FROM pg_opfamily",
        "FROM pg_ts_config",
        "FROM pg_ts_dict",
        "FROM pg_ts_parser",
        "FROM pg_ts_template",
        "FROM pg_statistic_ext",
        "FROM pg_constraint",
        "FROM pg_default_acl",
        "FROM pg_depend",
    ],
)
def test_empty_target_inspector_rejects_objects_and_unsupported_database_profiles(
    monkeypatch, blocking
):
    database = backup.PostgresDatabase(
        "postgresql://operator@localhost/unit_inspection"
    )
    connection = InspectionConnection(blocking=blocking)
    monkeypatch.setattr(database, "_inspect", lambda inspect: inspect(connection))
    with pytest.raises(backup.BackupError):
        database.empty_target_major()
    assert "left(nspname, 3) <> 'pg_'" in connection.queries[0]


@pytest.mark.parametrize("revisions", [[], ["future_m7"], ["003_m3", "004_m5"]])
def test_source_inspector_requires_one_known_real_schema_revision(
    monkeypatch, revisions
):
    database = backup.PostgresDatabase(
        "postgresql://operator@localhost/unit_inspection"
    )
    connection = InspectionConnection(revisions=revisions)
    monkeypatch.setattr(database, "_inspect", lambda inspect: inspect(connection))
    with pytest.raises(backup.BackupError, match="schema revision"):
        database.source_snapshot(backup.BackupLimits())


@pytest.mark.parametrize(
    "field,value",
    [
        ("files", None),
        ("files", []),
        ("storage_directories", {}),
        ("schema_revision", []),
        ("created_at", "not-a-date"),
    ],
)
def test_malformed_manifest_shapes_fail_closed(tmp_path, source, field, value):
    create(tmp_path, source)
    rewrite_manifest(
        tmp_path / "backup", lambda manifest: manifest.update({field: value})
    )
    with pytest.raises(backup.BackupError):
        backup.verify_backup(tmp_path / "backup")


@pytest.mark.parametrize(
    "field,value",
    [
        ("size_bytes", True),
        ("size_bytes", -1),
        ("sha256", "not-a-hash"),
        ("path", ["storage", "datasets"]),
    ],
)
def test_malformed_file_records_rejected(tmp_path, source, field, value):
    create(tmp_path, source)
    rewrite_manifest(
        tmp_path / "backup",
        lambda manifest: manifest["files"][1].update({field: value}),
    )
    with pytest.raises(backup.BackupError):
        backup.verify_backup(tmp_path / "backup")


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_duplicate_manifest_paths_rejected(tmp_path, source, kind):
    create(tmp_path, source)

    def duplicate(manifest):
        field = "files" if kind == "file" else "storage_directories"
        manifest[field].append(manifest[field][0])

    rewrite_manifest(tmp_path / "backup", duplicate)
    with pytest.raises(backup.BackupError, match="Duplicate"):
        backup.verify_backup(tmp_path / "backup")


def test_duplicate_json_fields_rejected(tmp_path, source):
    create(tmp_path, source)
    root = tmp_path / "backup"
    encoded = (
        (root / "manifest.json")
        .read_bytes()
        .replace(b'"format":', b'"format":"ignored","format":', 1)
    )
    (root / "manifest.json").write_bytes(encoded)
    (root / "COMPLETE").write_bytes(digest(encoded).encode() + b"\n")
    with pytest.raises(backup.BackupError):
        backup.verify_backup(root)


def test_custom_format_magic_is_required_even_with_matching_hashes(tmp_path, source):
    clients = FakeClients()
    clients.archive = b"not-a-custom-postgresql-archive"
    with pytest.raises(backup.BackupError, match="custom-format"):
        create(tmp_path, source, clients=clients)
    assert not (tmp_path / "backup/COMPLETE").exists()


def test_archive_change_during_private_spool_is_rejected_before_sql(
    tmp_path, source, monkeypatch
):
    create(tmp_path, source)
    real_open = backup._open_file
    changed = False

    def change_archive(root, key):
        nonlocal changed
        if (
            key == "database.dump"
            and (tmp_path / "restored/.restore-incomplete").exists()
            and not changed
        ):
            # Both re-verification and spool copying reject changes before SQL.
            changed = True
            path = root / key
            path.chmod(0o600)
            path.write_bytes(b"PGDMPchanged")
        return real_open(root, key)

    monkeypatch.setattr(backup, "_open_file", change_archive)
    clients = FakeClients()
    with pytest.raises(backup.BackupError):
        restore(tmp_path, source, clients=clients)
    assert not any(
        command[0] == "pg_restore" and command[-1] != "--version"
        for command in clients.calls
    )
    assert (tmp_path / "restored/.restore-incomplete").exists()


def test_fsync_failure_never_publishes_backup_complete_marker(
    tmp_path, source, monkeypatch
):
    def fail(fd):
        raise OSError("Injected fsync failure")

    monkeypatch.setattr(backup.os, "fsync", fail)
    with pytest.raises(OSError, match="fsync"):
        create(tmp_path, source)
    assert not (tmp_path / "backup/COMPLETE").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "exit",
        "timeout",
        "overflow",
        "interrupt",
        "short-write",
        "cleanup-race",
        "cleanup-timeout",
        "cleanup-denied",
        "exited-leader",
        "descendant-survives",
        "none",
    ],
)
def test_bounded_subprocess_protocol_without_starting_a_process(monkeypatch, failure):
    from types import SimpleNamespace

    class Process:
        def __init__(self):
            self.returncode = None
            self.stdout = io.BytesIO()
            self.pid = 12345
            self.waits = 0

        def poll(self):
            # Reaping before group cleanup permits PID reuse, and an exited
            # leader says nothing about descendants holding the output pipe.
            if failure == "exited-leader":
                return 0
            raise AssertionError("Do not reap the leader before group cleanup")

        def wait(self, timeout=None):
            self.waits += 1
            assert timeout is not None and 0 <= timeout <= 5
            if failure == "cleanup-timeout":
                raise backup.subprocess.TimeoutExpired("pg_dump", timeout)
            self.returncode = 2 if failure == "exit" else 0
            return self.returncode

    process = Process()
    launched = []
    killed = []
    probes = []

    def launch(command, **kwargs):
        launched.append((command, kwargs))
        assert kwargs["stderr"] == backup.subprocess.DEVNULL
        assert kwargs["start_new_session"] is True
        assert "shell" not in kwargs
        return process

    class Selector:
        def __enter__(self):
            self.active = False
            return self

        def __exit__(self, *args):
            return None

        def register(self, stream, flags):
            self.active = True

        def unregister(self, stream):
            self.active = False

        def get_map(self):
            return {101: True} if self.active else {}

        def select(self, timeout):
            return [(SimpleNamespace(fd=101, fileobj=process.stdout), 1)]

    class Sink(io.BytesIO):
        def write(self, value):
            if failure == "interrupt":
                raise KeyboardInterrupt
            if failure == "short-write":
                return len(value) - 1
            if failure.startswith("cleanup-") or failure == "descendant-survives":
                raise backup.BackupError("Injected client failure")
            return super().write(value)

    output = Sink()
    chunks = iter([b"PGDMPfixture", b""])
    ticks = 0

    def monotonic():
        nonlocal ticks
        value = ticks
        ticks += 2 if failure in {"timeout", "exited-leader"} else 0.01
        return value

    def waitid(kind, pid, options):
        assert kind == backup.os.P_PID and pid == process.pid
        assert options == backup.os.WEXITED | backup.os.WNOHANG | backup.os.WNOWAIT
        return SimpleNamespace(
            si_code=backup.os.CLD_EXITED, si_status=2 if failure == "exit" else 0
        )

    monkeypatch.setattr(backup.subprocess, "Popen", launch)
    monkeypatch.setattr(backup.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(backup.os, "read", lambda fd, size: next(chunks))
    monkeypatch.setattr(backup.os, "waitid", waitid)
    monkeypatch.setattr(backup.time, "monotonic", monotonic)
    monkeypatch.setattr(backup.time, "sleep", lambda seconds: None)

    def kill(pid, signal):
        assert pid == process.pid
        if signal == 0:
            assert process.waits == 1
            probes.append(pid)
            if failure == "descendant-survives":
                return
            raise ProcessLookupError("Owned group has exited")
        assert signal == backup.signal.SIGKILL
        assert process.waits == 0, "Leader must reserve its PID until group cleanup"
        killed.append(pid)
        if failure == "cleanup-race":
            raise ProcessLookupError("Process exited just before kill")
        if failure == "cleanup-denied":
            raise PermissionError("Injected cleanup uncertainty")

    monkeypatch.setattr(backup.os, "killpg", kill)

    credential = secrets.token_urlsafe(32)

    def run():
        backup.bounded_subprocess(
            ("pg_dump", "--format=custom"),
            env={"PGPASSWORD": credential},
            stdin=None,
            stdout=output,
            timeout=1,
            max_output_bytes=5 if failure == "overflow" else 50,
        )

    if failure == "none":
        run()
        assert output.getvalue() == b"PGDMPfixture"
    else:
        with pytest.raises(
            KeyboardInterrupt if failure == "interrupt" else backup.BackupError
        ) as error:
            run()
        if failure in {"cleanup-timeout", "cleanup-denied", "descendant-survives"}:
            assert "cleanup is uncertain" in str(error.value)
        elif failure == "cleanup-race":
            assert "Injected client failure" in str(error.value)
    assert len(launched) == 1
    contains_credential = credential in repr(launched[0][0])
    assert not contains_credential
    assert process.stdout.closed
    if failure in {
        "timeout",
        "overflow",
        "interrupt",
        "short-write",
        "cleanup-race",
        "cleanup-timeout",
        "cleanup-denied",
        "exit",
        "exited-leader",
        "descendant-survives",
    }:
        assert killed == [12345]
        assert probes and set(probes) == {12345}
        assert len(probes) <= 102, "Group-exit verification must remain bounded"
    else:
        assert not killed and not probes


def test_inventory_uses_streaming_directory_enumeration(tmp_path, source, monkeypatch):
    def forbid_eager_listing(path):
        raise AssertionError("Path.iterdir can eagerly list all directory entries")

    monkeypatch.setattr(type(tmp_path), "iterdir", forbid_eager_listing)
    create(tmp_path, source)
    backup.verify_backup(tmp_path / "backup")
    restore(tmp_path, source)


def test_registered_upload_missing_from_private_store_prevents_backup(tmp_path, source):
    (source[0] / "uploads/owner/source.csv").unlink()
    with pytest.raises(backup.BackupError, match="Database reference"):
        create(tmp_path, source)
    assert not (tmp_path / "backup").exists()


LIBPQ_SETTINGS = sorted(
    {
        option.envvar.decode("ascii")
        for option in backup.pq.Conninfo.get_defaults()
        if option.envvar is not None
    }
    | {"PGSERVICEFILE", "PGSYSCONFDIR"}
)


@pytest.mark.parametrize("setting", LIBPQ_SETTINGS)
@pytest.mark.parametrize("boundary", ["inspect", "client-environment"])
def test_each_recognized_ambient_libpq_setting_is_rejected_without_connection_or_value_disclosure(
    monkeypatch, setting, boundary
):
    # Construct first, then inject to prove checks occur at use boundaries rather
    # than only when the connection URL is initially accepted.
    credential, ambient = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    database = backup.PostgresDatabase(
        f"postgresql://operator:{credential}@127.0.0.1/explicit_db"
    )
    monkeypatch.setenv(setting, ambient)

    def forbidden(*args, **kwargs):
        raise AssertionError("Must reject ambient selectors before creating an engine")

    monkeypatch.setattr(backup, "create_engine", forbidden)
    with pytest.raises(backup.BackupError, match="Ambient libpq settings") as error:
        if boundary == "inspect":
            database.empty_target_major()
        else:
            database.environment()
    contains_credential = any(
        value in str(error.value) for value in (credential, ambient)
    )
    assert not contains_credential


@pytest.mark.parametrize(
    "url",
    [
        "postgresql:///database",
        "postgresql://operator@/database",
        "postgresql://localhost/database",
        "postgresql://operator@host1,host2/database",
        "postgresql://operator@host1:5432,host2:5433/database",
        "postgresql://operator@%2Fvar%2Frun%2Fpostgresql/database",
        "postgresql://operator@localhost:0/database",
        "postgresql://operator@localhost:65536/database",
        "postgresql://operator@localhost/database?host=other",
        "postgresql://operator@localhost/database?service=other",
        "postgresql://operator@localhost/database?hostaddr=127.0.0.2",
        "postgresql://operator@localhost/database?sslmode=disable&sslmode=require",
    ],
)
def test_missing_or_ambiguous_connection_selectors_are_rejected(url):
    with pytest.raises(backup.BackupError, match="connection configuration"):
        backup.PostgresDatabase(url)


@pytest.mark.parametrize(
    "address,expected_host,expected_port",
    [
        ("127.0.0.1", "127.0.0.1", 5432),
        ("postgres.example:6543", "postgres.example", 6543),
        ("[::1]:5434", "::1", 5434),
    ],
)
@pytest.mark.parametrize("sslmode", ["disable", "prefer", "require", "verify-full"])
def test_inspector_and_client_receive_identical_explicit_connection_identity(
    monkeypatch, address, expected_host, expected_port, sslmode
):
    credential = secrets.token_urlsafe(32)
    database = backup.PostgresDatabase(
        f"postgresql://operator:{credential}@{address}/unit_db?sslmode={sslmode}"
    )
    captured = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, statement):
            assert str(statement) == "SET TRANSACTION READ ONLY"

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            self.disposed = True

    def engine(url, **kwargs):
        captured.append((url, kwargs))
        return Engine()

    monkeypatch.setattr(backup, "create_engine", engine)
    assert (
        database._inspect(lambda connection: "read-only-inspection")
        == "read-only-inspection"
    )
    env = database.environment()
    url, kwargs = captured[0]
    assert url.host == env["PGHOST"] == expected_host
    assert url.port == int(env["PGPORT"]) == expected_port
    assert url.username == env["PGUSER"] == "operator"
    assert url.database == env["PGDATABASE"] == "unit_db"
    credential_matches = all(
        secrets.compare_digest(value, credential)
        for value in (url.password, env["PGPASSWORD"])
    )
    assert credential_matches
    assert kwargs["connect_args"]["passfile"] == env["PGPASSFILE"] == "/dev/null"
    assert (
        kwargs["connect_args"]["sslmode"]
        == url.query["sslmode"]
        == env["PGSSLMODE"]
        == sslmode
    )
    assert (
        kwargs["connect_args"]["connect_timeout"] == int(env["PGCONNECT_TIMEOUT"]) == 5
    )
    assert kwargs["hide_parameters"] is True


def test_client_executable_overrides_are_not_libpq_settings(monkeypatch):
    monkeypatch.setenv("PG_DUMP_EXECUTABLE", "/trusted/pg-dump-wrapper")
    monkeypatch.setenv("PG_RESTORE_EXECUTABLE", "/trusted/pg-restore-wrapper")
    database = backup.PostgresDatabase("postgresql://operator@127.0.0.1/unit_db")
    env = database.environment()
    assert env["PGPORT"] == "5432"
    assert "PGPASSWORD" not in env
    assert "PG_DUMP_EXECUTABLE" not in env
    assert "PG_RESTORE_EXECUTABLE" not in env


def test_unknown_or_failed_libpq_metadata_fails_closed(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        backup, "pq", SimpleNamespace(Conninfo=SimpleNamespace(get_defaults=list))
    )
    database = backup.PostgresDatabase("postgresql://operator@127.0.0.1/unit_db")
    with pytest.raises(backup.BackupError, match="metadata"):
        database.environment()


def test_empty_target_rejects_global_default_acl_without_public_objects(monkeypatch):
    database = backup.PostgresDatabase("postgresql://operator@127.0.0.1/unit_db")
    connection = InspectionConnection()
    original = connection.execute

    def execute(statement, parameters=None):
        if str(statement) == "SELECT count(*) FROM pg_default_acl":
            return QueryResult(1)  # namespace 0 only; all public checks are empty.
        return original(statement, parameters)

    connection.execute = execute
    monkeypatch.setattr(database, "_inspect", lambda inspect: inspect(connection))
    with pytest.raises(backup.BackupError, match="EMPTY database"):
        database.empty_target_major()


@pytest.mark.parametrize("custom_home", [True, False])
def test_native_client_preserves_only_effective_home_for_default_ssl_paths(
    monkeypatch, custom_home
):
    from pathlib import Path

    if custom_home:
        monkeypatch.setenv("HOME", "/private/synthetic-ssl-home")
    else:
        monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("CLOUD_PROVIDER_TOKEN", secrets.token_urlsafe(32))
    database = backup.PostgresDatabase(
        "postgresql://operator@127.0.0.1/unit_db?sslmode=require"
    )
    environment = database.environment()
    assert environment["HOME"] == str(Path.home())
    assert "CLOUD_PROVIDER_TOKEN" not in environment
    assert "synthetic-ssl-home" not in repr(database)
    assert "HOME" not in database._url.query


@pytest.mark.parametrize("home", ["", "relative-home"])
def test_relative_home_profile_fails_before_inspection_or_client_invocation(
    monkeypatch,
    home,
):
    monkeypatch.setenv("HOME", home)
    database = backup.PostgresDatabase("postgresql://operator@127.0.0.1/unit_db")

    def forbidden(*args, **kwargs):
        raise AssertionError("must reject the ambiguous home profile first")

    monkeypatch.setattr(backup, "create_engine", forbidden)
    with pytest.raises(backup.BackupError, match="absolute path"):
        database.environment()
    with pytest.raises(backup.BackupError, match="absolute path"):
        database.empty_target_major()


@pytest.mark.parametrize(
    "revision",
    [
        "003_m3",
        "004_m5",
        "005_settings",
        "006_provider_connections",
        "007_research_specs",
        "008_comparisons",
        "009_independent_evaluations",
        "010_research_notes",
        "011_note_pin_trigger",
    ],
)
def test_supported_revisions_backup_roundtrip_without_relabeling(
    tmp_path, source, revision
):
    source[2]["schema_revision"] = revision
    clients = FakeClients()
    manifest = create(tmp_path, source, clients=clients)
    assert manifest["schema_revision"] == revision
    assert restore(tmp_path, source, clients=clients) == manifest


@pytest.mark.parametrize(
    "revision",
    [
        "004_m5",
        "005_settings",
        "006_provider_connections",
        "007_research_specs",
        "008_comparisons",
        "009_independent_evaluations",
        "010_research_notes",
        "011_note_pin_trigger",
    ],
)
def test_source_inventory_retains_strategy_packages_after_settings_revision(
    monkeypatch, revision
):
    database = backup.PostgresDatabase(
        "postgresql://operator@localhost/unit_inspection"
    )
    connection = InspectionConnection(revisions=[revision])
    monkeypatch.setattr(database, "_inspect", lambda inspect: inspect(connection))
    assert (
        database.source_snapshot(backup.BackupLimits())["schema_revision"] == revision
    )
    assert any("FROM strategy_packages" in query for query in connection.queries)


@pytest.mark.parametrize("revision", ["010_research_notes", "011_note_pin_trigger"])
def test_note_revision_pins_survive_missing_live_artifact_registration(
    monkeypatch, revision
):
    class NotesConnection(InspectionConnection):
        def execute(self, statement, parameters=None):
            result = super().execute(statement, parameters)
            if "FROM research_note_evidence_pins" in str(statement):
                return QueryResult(
                    [
                        {
                            "path": "runs/historical/artifact.json",
                            "sha256": "a" * 64,
                            "size_bytes": 12,
                        },
                        {
                            "path": "runs/historical/artifact.json",
                            "sha256": "a" * 64,
                            "size_bytes": 12,
                        },
                    ]
                )
            return result

    connection = NotesConnection(revisions=[revision])
    database = backup.PostgresDatabase(
        "postgresql://operator@localhost/unit_inspection"
    )
    monkeypatch.setattr(database, "_inspect", lambda inspect: inspect(connection))
    snapshot = database.source_snapshot(backup.BackupLimits())
    assert snapshot["references"] == [
        {"path": "runs/historical/artifact.json", "sha256": "a" * 64, "size_bytes": 12}
    ]
    query = next(
        q for q in connection.queries if "FROM research_note_evidence_pins" in q
    )
    assert "JOIN" not in query and "DISTINCT" in query
    assert "WHERE resource_kind='artifact'" in query


@pytest.mark.parametrize("revision", ["010_research_notes", "011_note_pin_trigger"])
def test_conflicting_historical_pin_does_not_get_deduplicated(monkeypatch, revision):
    class NotesConnection(InspectionConnection):
        def execute(self, statement, parameters=None):
            result = super().execute(statement, parameters)
            if "FROM research_note_evidence_pins" in str(statement):
                return QueryResult(
                    [
                        {
                            "path": "runs/historical/artifact.json",
                            "sha256": h * 64,
                            "size_bytes": 12,
                        }
                        for h in ("a", "b")
                    ]
                )
            return result

    connection = NotesConnection(revisions=[revision])
    database = backup.PostgresDatabase(
        "postgresql://operator@localhost/unit_inspection"
    )
    monkeypatch.setattr(database, "_inspect", lambda inspect: inspect(connection))
    snapshot = database.source_snapshot(backup.BackupLimits())
    assert len(snapshot["references"]) == 2
    with pytest.raises(backup.BackupError, match="missing or differs"):
        backup._validate_references(
            None, [snapshot["references"][0]], snapshot, backup.BackupLimits()
        )
