"""Bounded, private, quiesced logical backup. Not a hot-backup or PITR system.

Only trusted operators, paths, databases and official PostgreSQL client binaries
are supported. Callers must stop ALL writers first. Hashes establish integrity,
not authenticity; restoring a PostgreSQL archive executes its trusted SQL.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from psycopg import Error as PsycopgError
from psycopg import pq
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from backend.app.core.atomic_storage import fsync_directory, provision_directory

FORMAT = "tradegrad.cold-backup.v1"
REVISIONS = {
    "003_m3",
    "004_m5",
    "005_settings",
    "006_provider_connections",
    "007_research_specs",
    "008_comparisons",
    "009_independent_evaluations",
    "010_research_notes",
    "011_note_pin_trigger",
}
NAMESPACES = {"datasets", "runs", "data-checkpoints", "uploads", "strategy-packages"}
HEX = re.compile(r"[a-f0-9]{64}\Z")
APP_REF = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
CHUNK = 1024 * 1024


class BackupError(ValueError):
    """Safe operator-facing error. Never embed a URL, SQL or archive content."""


@dataclass(frozen=True)
class BackupLimits:
    max_files: int = 100_000
    max_file_bytes: int = 2_000_000_000
    max_total_bytes: int = 20_000_000_000
    max_dump_bytes: int = 10_000_000_000
    max_manifest_bytes: int = 32_000_000
    timeout_seconds: int = 600

    def __post_init__(self):
        for value in vars(self).values():
            if type(value) is not int or value <= 0:
                raise BackupError("Limits must be positive integers")


class CommandRunner(Protocol):
    def __call__(
        self,
        command: tuple[str, ...],
        *,
        env: dict[str, str],
        stdin: BinaryIO | None,
        stdout: BinaryIO,
        timeout: int,
        max_output_bytes: int,
    ) -> None: ...


def bounded_subprocess(
    command: tuple[str, ...],
    *,
    env: dict[str, str],
    stdin: BinaryIO | None,
    stdout: BinaryIO,
    timeout: int,
    max_output_bytes: int,
) -> None:
    """No shell, no stderr/credential logging, bounded streaming and timeout.

    stderr is deliberately discarded rather than risk printing database values.
    The subprocess exit code is still mandatory. Diagnostics require an operator
    in a private environment. Killing a remote wrapper is not proof of remote
    rollback, so every failed restore remains explicitly incomplete.
    """
    process = None
    complete = False
    try:
        process = subprocess.Popen(
            command,
            stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        assert process.stdout is not None
        deadline = time.monotonic() + timeout
        written = 0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BackupError("PostgreSQL client timed out")
                for key, _ in selector.select(min(remaining, 0.2)):
                    chunk = os.read(key.fd, CHUNK)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        break
                    written += len(chunk)
                    if written > max_output_bytes:
                        raise BackupError("PostgreSQL client output exceeds its limit")
                    if stdout.write(chunk) != len(chunk):
                        raise BackupError("Archive write was incomplete")
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BackupError("PostgreSQL client timed out")
                # Observe without reaping: an exited leader still reserves its
                # PID/process-group identity until abnormal cleanup signals it.
                status = os.waitid(
                    os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
                if status is not None:
                    if status.si_code != os.CLD_EXITED or status.si_status != 0:
                        raise BackupError(
                            "PostgreSQL client failed; no complete result"
                        )
                    complete = True
                    break
                time.sleep(min(remaining, 0.05))
    except (OSError, subprocess.SubprocessError):
        raise BackupError("PostgreSQL client could not complete") from None
    finally:
        if process is not None:
            try:
                uncertain = False
                cleanup_deadline = time.monotonic() + min(timeout, 5)
                if not complete:
                    try:
                        # Do not poll/wait first. A wrapper may already have
                        # exited while its descendants still hold the pipe.
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except OSError:
                        uncertain = True
                try:
                    process.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
                except (OSError, subprocess.SubprocessError):
                    uncertain = True
                if not complete:
                    # Reaping the leader does not prove descendant exit. Only
                    # probe after reaping; never signal a potentially reused ID.
                    while True:
                        try:
                            os.killpg(process.pid, 0)
                        except ProcessLookupError:
                            break
                        except OSError:
                            uncertain = True
                            break
                        remaining = cleanup_deadline - time.monotonic()
                        if remaining <= 0:
                            uncertain = True
                            break
                        time.sleep(min(remaining, 0.05))
                if uncertain:
                    raise BackupError(
                        "PostgreSQL client cleanup is uncertain; keep the target incomplete"
                    ) from None
            finally:
                if process.stdout is not None:
                    process.stdout.close()


@dataclass(frozen=True)
class PgTools:
    pg_dump: tuple[str, ...] = ("pg_dump",)
    pg_restore: tuple[str, ...] = ("pg_restore",)
    runner: CommandRunner = bounded_subprocess

    def check_versions(self, env: dict[str, str], major: int, limits: BackupLimits):
        for command, expected in (
            (self.pg_dump, "pg_dump"),
            (self.pg_restore, "pg_restore"),
        ):
            output = io.BytesIO()
            self.runner(
                command + ("--version",),
                env=env,
                stdin=None,
                stdout=output,
                timeout=min(limits.timeout_seconds, 10),
                max_output_bytes=1024,
            )
            match = re.fullmatch(
                rf"{expected} \(PostgreSQL\) (\d+)\.\d+[^\r\n]*\n?",
                output.getvalue().decode("ascii", errors="replace"),
            )
            if not match or int(match[1]) != major:
                raise BackupError("PostgreSQL client/server major version mismatch")


class Database(Protocol):
    database_name: str

    def environment(self) -> dict[str, str]: ...
    def source_snapshot(self, limits: BackupLimits) -> dict: ...
    def empty_target_major(self) -> int: ...


def _reject_ambient_libpq_settings():
    # Ask the installed libpq which environment selectors it recognizes. Never
    # inspect, interpolate or report their values. Executable override variables
    # such as PG_DUMP_EXECUTABLE are not libpq settings and remain usable.
    try:
        names = {
            option.envvar.decode("ascii")
            for option in pq.Conninfo.get_defaults()
            if option.envvar is not None
        }
    except (PsycopgError, ValueError, UnicodeError, RuntimeError):
        raise BackupError(
            "Could not establish the libpq connection environment profile"
        ) from None
    if (
        not {
            "PGHOST",
            "PGPORT",
            "PGUSER",
            "PGDATABASE",
            "PGPASSWORD",
            "PGPASSFILE",
            "PGSERVICE",
        }
        <= names
    ):
        raise BackupError("Unsupported libpq connection metadata")
    # These libpq service-file location controls are not connection options, so
    # PQconndefaults does not enumerate them.
    names.update({"PGSERVICEFILE", "PGSYSCONFDIR"})
    if names.intersection(os.environ):
        raise BackupError(
            "Ambient libpq settings are forbidden; use one explicit PostgreSQL URL in a clean operator process"
        )


def _effective_home() -> str:
    # libpq uses the user's home for default SSL certificate paths. Keep native
    # inspection/client lookup consistent without forwarding any SSL credentials.
    configured = os.environ.get("HOME")
    if configured is not None and (
        not configured or not Path(configured).is_absolute()
    ):
        raise BackupError("Native client home must be an absolute path")
    try:
        value = str(Path.home())
    except (KeyError, RuntimeError):
        raise BackupError("Cannot establish the native client home profile") from None
    if not value or not Path(value).is_absolute() or "\x00" in value:
        raise BackupError("Native client home must be an absolute path")
    return value


def _single_tcp_host(value: str | None) -> bool:
    if not value or len(value) > 253 or any(char in value for char in ",/%@\\"):
        return False
    if ":" in value:
        try:
            return isinstance(ipaddress.ip_address(value), ipaddress.IPv6Address)
        except ValueError:
            return False
    return (
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.?", value) is not None
    )


class PostgresDatabase:
    """Short-lived, bounded read-only database inspection, never DB creation/drop."""

    def __init__(self, database_url: str):
        try:
            url = make_url(database_url)
            if url.drivername not in {"postgresql", "postgresql+psycopg"}:
                raise ValueError
            if not url.database or not IDENTIFIER.fullmatch(url.database):
                raise ValueError
            if (
                not _single_tcp_host(url.host)
                or not url.username
                or not IDENTIFIER.fullmatch(url.username)
            ):
                raise ValueError
            port = 5432 if url.port is None else url.port
            if not 1 <= port <= 65535:
                raise ValueError
            if set(url.query) - {"sslmode"}:
                raise ValueError
            if url.query.get("sslmode", "prefer") not in {
                "disable",
                "allow",
                "prefer",
                "require",
                "verify-ca",
                "verify-full",
            }:
                raise ValueError
            self._url = url.set(
                drivername="postgresql+psycopg",
                port=port,
                query={"sslmode": url.query.get("sslmode", "prefer")},
            )
            self.database_name = url.database
        except (ArgumentError, TypeError, ValueError):
            raise BackupError(
                "Unsupported PostgreSQL connection configuration"
            ) from None

    def environment(self) -> dict[str, str]:
        _reject_ambient_libpq_settings()
        # Do not inherit ambient PG*, service files, app secrets or cloud tokens.
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "HOME": _effective_home(),
        }
        env.update(
            PGDATABASE=self.database_name,
            PGCONNECT_TIMEOUT="5",
            PGPASSFILE="/dev/null",
            PGOPTIONS="-c statement_timeout=120000 -c search_path=public",
        )
        for key, value in (
            ("PGHOST", self._url.host),
            ("PGPORT", self._url.port),
            ("PGUSER", self._url.username),
            ("PGPASSWORD", self._url.password),
        ):
            if value is not None:
                env[key] = str(value)
        env["PGSSLMODE"] = str(self._url.query.get("sslmode", "prefer"))
        return env

    def _inspection_engine(self):
        _reject_ambient_libpq_settings()
        _effective_home()
        return create_engine(
            self._url,
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 5,
                "passfile": "/dev/null",
                "sslmode": self._url.query["sslmode"],
                "options": "-c statement_timeout=10000 -c search_path=public",
            },
        )

    def _inspect(self, function):
        engine = self._inspection_engine()
        try:
            with engine.connect() as connection:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                return function(connection)
        except BackupError:
            raise
        except (SQLAlchemyError, OSError):
            raise BackupError(
                "PostgreSQL inspection failed; no complete result"
            ) from None
        finally:
            engine.dispose()

    @staticmethod
    def _profile(connection) -> int:
        extra = connection.execute(
            text("""
            SELECT count(*) FROM pg_namespace
            WHERE nspname NOT IN ('public', 'information_schema')
              AND left(nspname, 3) <> 'pg_'
        """)
        ).scalar_one()
        if (
            extra
            or connection.execute(
                text("SELECT count(*) FROM pg_largeobject_metadata")
            ).scalar_one()
        ):
            raise BackupError(
                "Only public-schema databases without large objects are supported"
            )
        if connection.execute(
            text("SELECT count(*) FROM pg_extension WHERE extname <> 'plpgsql'")
        ).scalar_one():
            raise BackupError(
                "Additional PostgreSQL extensions require a separate recovery profile"
            )
        for catalog in (
            "pg_event_trigger",
            "pg_foreign_server",
            "pg_foreign_data_wrapper",
            "pg_publication",
            "pg_subscription",
        ):
            if connection.execute(text(f"SELECT count(*) FROM {catalog}")).scalar_one():
                raise BackupError(
                    "Foreign access, replication and event triggers require a separate recovery profile"
                )
        return (
            int(connection.execute(text("SHOW server_version_num")).scalar_one())
            // 10000
        )

    def empty_target_major(self) -> int:
        def inspect(connection):
            major = self._profile(connection)
            namespace_columns = (
                ("pg_class", "relnamespace"),
                ("pg_proc", "pronamespace"),
                ("pg_type", "typnamespace"),
                ("pg_collation", "collnamespace"),
                ("pg_conversion", "connamespace"),
                ("pg_operator", "oprnamespace"),
                ("pg_opclass", "opcnamespace"),
                ("pg_opfamily", "opfnamespace"),
                ("pg_ts_config", "cfgnamespace"),
                ("pg_ts_dict", "dictnamespace"),
                ("pg_ts_parser", "prsnamespace"),
                ("pg_ts_template", "tmplnamespace"),
                ("pg_statistic_ext", "stxnamespace"),
                ("pg_constraint", "connamespace"),
                ("pg_default_acl", "defaclnamespace"),
            )
            # Database-local global defaults (namespace 0) can grant access to
            # newly restored objects despite --no-privileges. Reject all defaults.
            objects = connection.execute(
                text("SELECT count(*) FROM pg_default_acl")
            ).scalar_one()
            for catalog, column in namespace_columns:
                objects += connection.execute(
                    text(
                        f"SELECT count(*) FROM {catalog} o JOIN pg_namespace n "
                        f"ON n.oid=o.{column} WHERE n.nspname='public'"
                    )
                ).scalar_one()
            # Catch namespace-owned object classes added by future PostgreSQL
            # versions as well as the explicitly inspected known catalogs.
            objects += connection.execute(
                text(
                    "SELECT count(*) FROM pg_depend WHERE refclassid='pg_namespace'::regclass "
                    "AND refobjid=(SELECT oid FROM pg_namespace WHERE nspname='public')"
                )
            ).scalar_one()
            if objects:
                raise BackupError(
                    "Restore requires an EMPTY database; existing objects are forbidden"
                )
            return major

        return self._inspect(inspect)

    def source_snapshot(self, limits: BackupLimits) -> dict:
        def inspect(connection):
            major = self._profile(connection)
            revisions = (
                connection.execute(text("SELECT version_num FROM alembic_version"))
                .scalars()
                .all()
            )
            if len(revisions) != 1 or revisions[0] not in REVISIONS:
                raise BackupError(
                    "Unsupported or ambiguous application schema revision"
                )
            checks = (
                "SELECT count(*) FROM research_runs WHERE status NOT IN ('paused','cancelled','failed','interrupted','succeeded')",
                "SELECT count(*) FROM execution_attempts WHERE NOT sandbox_stopped OR finished_at IS NULL OR status NOT IN ('paused','cancelled','failed','interrupted','succeeded')",
                "SELECT count(*) FROM research_evaluations WHERE NOT sandbox_stopped OR status='registered'",
                "SELECT count(*) FROM data_imports WHERE status NOT IN ('ready','partial','failed','cancelled') OR lease_token IS NOT NULL OR lease_expires_at IS NOT NULL",
                "SELECT count(*) FROM procrastinate_jobs WHERE status IN ('todo','doing')",
            )
            if any(connection.execute(text(query)).scalar_one() for query in checks):
                raise BackupError(
                    "Active, queued, leased or unstopped execution/imports must be resolved first"
                )

            def rows(query):
                result = [
                    dict(row)
                    for row in connection.execute(
                        text(query + " LIMIT :limit"), {"limit": limits.max_files + 1}
                    ).mappings()
                ]
                if len(result) > limits.max_files:
                    raise BackupError("Database reference inventory exceeds its limit")
                return result

            references = rows(
                "SELECT storage_key AS path, sha256, size_bytes FROM artifacts ORDER BY storage_key"
            )
            references += rows(
                "SELECT storage_key AS path, content_sha256 AS sha256 FROM data_blocks WHERE storage_key IS NOT NULL ORDER BY storage_key"
            )
            references += rows(
                "SELECT request->'upload'->>'storage_key' AS path, "
                "request->'upload'->>'sha256' AS sha256, "
                "(request->'upload'->>'size_bytes')::bigint AS size_bytes "
                "FROM data_imports WHERE kind='upload' ORDER BY id"
            )
            checkpoints = rows(
                "SELECT manifest_storage_key AS path, manifest_sha256 AS sha256 FROM research_checkpoints ORDER BY manifest_storage_key"
            )
            references += checkpoints
            if revisions[0] in {
                "004_m5",
                "005_settings",
                "006_provider_connections",
                "007_research_specs",
                "008_comparisons",
                "009_independent_evaluations",
                "010_research_notes",
                "011_note_pin_trigger",
            }:
                references += rows(
                    "SELECT storage_key AS path, archive_sha256 AS sha256, size_bytes FROM strategy_packages ORDER BY storage_key"
                )
            if revisions[0] in {"010_research_notes", "011_note_pin_trigger"}:
                # Historical note revisions retain exact artifacts independently
                # of a mutable/current resource pointer. Never JOIN these pins to
                # artifacts: deliberate metadata cleanup must not silently drop
                # the original byte-retention obligation. Missing or changed
                # bytes fail the existing immutable-reference integrity checks.
                references += rows(
                    "SELECT DISTINCT details->>'storage_key' AS path, sha256, "
                    "(details->>'size_bytes')::bigint AS size_bytes "
                    "FROM research_note_evidence_pins "
                    "WHERE resource_kind='artifact' "
                    "ORDER BY path, sha256, size_bytes"
                )
                # Exact duplicates across revisions and registration inventory
                # are one file, while same-path/different-hash conflicts remain
                # separate and are rejected by byte verification below.
                references = list(
                    {
                        (row["path"], row["sha256"], row.get("size_bytes")): row
                        for row in references
                    }.values()
                )
            datasets = rows(
                "SELECT storage_key AS path, content_sha256 AS sha256, manifest FROM dataset_versions ORDER BY storage_key"
            )
            if len(references) + len(datasets) > limits.max_files:
                raise BackupError("Database reference inventory exceeds its limit")
            return {
                "schema_revision": revisions[0],
                "postgres_major": major,
                "references": references,
                "datasets": datasets,
                "checkpoints": checkpoints,
            }

        return self._inspect(inspect)


def _path(path: Path) -> Path:
    if ".." in path.parts:
        raise BackupError("Parent traversal is forbidden")
    return path.absolute()


def _directory(path: Path) -> Path:
    path = _path(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise BackupError("Directories must exist and must not be symlinks")
    return path


def _key(value: str, *, storage: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 1500:
        raise BackupError("Invalid manifest path")
    parts = value.split("/")
    if any(not p or p in {".", ".."} or len(p.encode()) > 255 for p in parts):
        raise BackupError("Unsafe manifest path")
    if "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise BackupError("Unsafe manifest path")
    if storage and parts[0] not in NAMESPACES:
        raise BackupError(
            "Unsupported storage namespace; secrets and runner state are not backup inputs"
        )
    return value


def _new_directory(path: Path, parent: Path) -> Path:
    parent = _directory(parent)
    path = _path(path)
    if path == parent or not path.is_relative_to(parent):
        raise BackupError("New destination must be below its explicit durable parent")
    _directory(path.parent)
    try:
        path.mkdir(mode=0o700)  # Never exist_ok, never repair/reuse a partial target.
    except FileExistsError:
        raise BackupError("Destination already exists; use a new path") from None
    provision_directory(path, durable_parent=parent)
    return path


def _open_file(root: Path, key: str):
    path = root / _key(key)
    _directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise BackupError("Only independent regular files are supported")
    return os.fdopen(fd, "rb"), info


def _hash_file(root: Path, key: str, maximum: int) -> dict:
    stream, info = _open_file(root, key)
    with stream:
        if info.st_size > maximum:
            raise BackupError("File exceeds backup byte limit")
        digest = hashlib.sha256()
        size = 0
        while chunk := stream.read(min(CHUNK, maximum - size + 1)):
            size += len(chunk)
            if size > maximum:
                raise BackupError("File exceeds backup byte limit")
            digest.update(chunk)
        final = os.fstat(stream.fileno())
        if size != info.st_size or (
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ) != (info.st_size, info.st_mtime_ns, info.st_ctime_ns):
            raise BackupError("Source changed while reading; keep all writers stopped")
        return {"path": key, "size_bytes": size, "sha256": digest.hexdigest()}


def _inventory(
    root: Path, limits: BackupLimits, *, storage: bool
) -> tuple[list[dict], list[str]]:
    _directory(root)
    entries, directories, pending = [], [], [root]
    total = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                path = Path(child.path)
                key = path.relative_to(root).as_posix()
                _key(key, storage=storage)
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    directories.append(key)
                    if len(directories) > limits.max_files * 2 + 16 + (
                        0 if storage else 1
                    ):
                        raise BackupError("Directory inventory exceeds its limit")
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    if len(entries) >= limits.max_files + (0 if storage else 3):
                        raise BackupError("File inventory exceeds its limit")
                    maximum = limits.max_file_bytes
                    if not storage:
                        maximum = (
                            limits.max_dump_bytes
                            if key == "database.dump"
                            else limits.max_file_bytes
                        )
                        if key in {"manifest.json", "COMPLETE"}:
                            maximum = limits.max_manifest_bytes
                    item = _hash_file(root, key, maximum)
                    total += item["size_bytes"]
                    if total > limits.max_total_bytes + (
                        limits.max_manifest_bytes * 2 if not storage else 0
                    ):
                        raise BackupError("Backup total bytes exceed limit")
                    entries.append(item)
                else:
                    raise BackupError(
                        "Symlinks, devices, sockets and special files are forbidden"
                    )
    return sorted(entries, key=lambda item: item["path"]), sorted(directories)


def _canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def _read_json(root: Path, key: str, limits: BackupLimits):
    stream, info = _open_file(root, key)
    with stream:
        if info.st_size > limits.max_manifest_bytes:
            raise BackupError("Manifest exceeds byte limit")
        content = stream.read(limits.max_manifest_bytes + 1)
    if len(content) > limits.max_manifest_bytes:
        raise BackupError("Manifest exceeds byte limit")

    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise BackupError("Duplicate manifest field")
            result[name] = value
        return result

    try:
        return json.loads(content, object_pairs_hook=unique), content
    except (ValueError, UnicodeError, RecursionError):
        raise BackupError("Invalid JSON manifest") from None


def _validate_references(
    root: Path, inventory: list[dict], snapshot: dict, limits: BackupLimits
):
    by_key = {entry["path"]: entry for entry in inventory}

    def check(key, digest, size=None):
        _key(key, storage=True)
        if not isinstance(digest, str) or not HEX.fullmatch(digest):
            raise BackupError("Invalid immutable database reference hash")
        entry = by_key.get(key)
        if (
            entry is None
            or entry["sha256"] != digest
            or (size is not None and entry["size_bytes"] != size)
        ):
            raise BackupError(
                "Database reference is missing or differs from immutable storage bytes"
            )

    for item in snapshot["references"]:
        check(item["path"], item["sha256"], item.get("size_bytes"))
    for item in snapshot["datasets"]:
        base = _key(item["path"], storage=True)
        manifest_key = f"{base}/manifest.json"
        check(manifest_key, item["sha256"])
        manifest, _ = _read_json(root, manifest_key, limits)
        if not isinstance(manifest, dict) or manifest != item["manifest"]:
            raise BackupError("Dataset manifest differs from database reference")
        for field in ("files", "artifact_files"):
            values = manifest.get(field, {})
            if not isinstance(values, dict):
                raise BackupError("Unsupported dataset file inventory")
            for name, digest in values.items():
                check(f"{base}/{_key(name)}", digest)
        previews = manifest.get("preview_files", {})
        if not isinstance(previews, dict):
            raise BackupError("Unsupported dataset preview inventory")
        for values in previews.values():
            if not isinstance(values, list):
                raise BackupError("Unsupported dataset preview inventory")
            for entry in values:
                if not isinstance(entry, dict) or not {"path", "sha256"} <= set(entry):
                    raise BackupError("Invalid dataset preview reference")
                check(f"{base}/{_key(entry['path'])}", entry["sha256"])
    for item in snapshot["checkpoints"]:
        manifest, _ = _read_json(root, item["path"], limits)
        if not isinstance(manifest, dict) or not isinstance(
            manifest.get("files"), list
        ):
            raise BackupError("Unsupported checkpoint file inventory")
        for entry in manifest["files"]:
            if not isinstance(entry, dict) or not {
                "path",
                "sha256",
                "size_bytes",
            } <= set(entry):
                raise BackupError("Invalid checkpoint file reference")
            if type(entry["size_bytes"]) is not int or entry["size_bytes"] < 0:
                raise BackupError("Invalid checkpoint size")
            base = PurePosixPath(item["path"]).parent.as_posix()
            check(f"{base}/{_key(entry['path'])}", entry["sha256"], entry["size_bytes"])


def _write_new(path: Path, content: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        if stream.write(content) != len(content):
            raise BackupError("Metadata write was incomplete")
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(path.parent)


def _copy_stream(source: BinaryIO, destination: BinaryIO, item: dict):
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(min(CHUNK, item["size_bytes"] - size + 1)):
        size += len(chunk)
        if size > item["size_bytes"]:
            raise BackupError("Source grew during copy")
        digest.update(chunk)
        if destination.write(chunk) != len(chunk):
            raise BackupError("Storage copy was incomplete")
    if size != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
        raise BackupError("Source changed or copy integrity failed")
    destination.flush()
    os.fsync(destination.fileno())


def _copy(root: Path, target: Path, item: dict, durable_root: Path):
    provision_directory(target.parent, durable_parent=durable_root)
    source, _ = _open_file(root, item["path"])
    with source:
        fd = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "wb") as destination:
            _copy_stream(source, destination, item)
            os.fchmod(destination.fileno(), 0o400)
            os.fsync(destination.fileno())
    fsync_directory(target.parent)


def _application_ref(value: str):
    if not isinstance(value, str) or not APP_REF.fullmatch(value):
        raise BackupError("Application reference must be an exact lowercase Git SHA")


def _shared_storage_reader(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        from backend.app.core.storage_barrier import storage_guard

        if kwargs.get("quiesced") is not True:
            raise BackupError(
                "Explicit acknowledgement that API, all workers and writers are stopped is required"
            )
        root = _directory(kwargs["storage_root"])
        with storage_guard(root):
            return function(*args, **kwargs)

    return guarded


@_shared_storage_reader
def create_backup(
    *,
    database: Database,
    storage_root: Path,
    destination: Path,
    durable_parent: Path,
    application_ref: str,
    quiesced: bool = False,
    tools: PgTools | None = None,
    limits: BackupLimits | None = None,
) -> dict:
    if quiesced is not True:
        raise BackupError(
            "Explicit acknowledgement that API, all workers and writers are stopped is required"
        )
    _application_ref(application_ref)
    limits, tools = limits or BackupLimits(), tools or PgTools()
    storage_root = _directory(storage_root)
    destination = _path(destination)
    if destination.is_relative_to(storage_root) or storage_root.is_relative_to(
        destination
    ):
        raise BackupError("Backup and storage trees must be disjoint")
    snapshot = database.source_snapshot(limits)
    environment = database.environment()
    tools.check_versions(environment, snapshot["postgres_major"], limits)
    entries, directories = _inventory(storage_root, limits, storage=True)
    _validate_references(storage_root, entries, snapshot, limits)
    destination = _new_directory(destination, durable_parent)
    provision_directory(destination / "storage", durable_parent=destination)
    dump = destination / "database.dump"
    fd = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        tools.runner(
            tools.pg_dump
            + (
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--no-tablespaces",
                "--no-password",
            ),
            env=environment,
            stdin=None,
            stdout=stream,
            timeout=limits.timeout_seconds,
            max_output_bytes=limits.max_dump_bytes,
        )
        stream.flush()
        os.fchmod(stream.fileno(), 0o400)
        os.fsync(stream.fileno())
    dump_entry = _hash_file(destination, "database.dump", limits.max_dump_bytes)
    with dump.open("rb") as stream:
        if stream.read(5) != b"PGDMP":
            raise BackupError(
                "PostgreSQL client did not produce a custom-format archive"
            )
    if (
        dump_entry["size_bytes"] + sum(item["size_bytes"] for item in entries)
        > limits.max_total_bytes
    ):
        raise BackupError("Backup total bytes exceed limit")
    for directory in directories:
        provision_directory(
            destination / "storage" / directory, durable_parent=destination
        )
    for item in entries:
        _copy(storage_root, destination / "storage" / item["path"], item, destination)
    if snapshot != database.source_snapshot(limits) or (
        entries,
        directories,
    ) != _inventory(storage_root, limits, storage=True):
        raise BackupError("Source changed during backup; keep all writers stopped")
    manifest = {
        "format": FORMAT,
        "schema_revision": snapshot["schema_revision"],
        "application_ref": application_ref,
        "postgres_major": snapshot["postgres_major"],
        "created_at": datetime.now(UTC).isoformat(),
        "storage_directories": directories,
        "files": [dump_entry]
        + [{**item, "path": "storage/" + item["path"]} for item in entries],
    }
    content = _canonical(manifest)
    if len(content) > limits.max_manifest_bytes:
        raise BackupError("Manifest exceeds byte limit")
    _write_new(destination / "manifest.json", content)
    _write_new(
        destination / "COMPLETE", hashlib.sha256(content).hexdigest().encode() + b"\n"
    )
    verify_backup(destination, application_ref=application_ref, limits=limits)
    return manifest


def verify_backup(
    backup_root: Path,
    *,
    application_ref: str | None = None,
    limits: BackupLimits | None = None,
) -> dict:
    limits = limits or BackupLimits()
    root = _directory(backup_root)
    manifest, content = _read_json(root, "manifest.json", limits)
    stream, info = _open_file(root, "COMPLETE")
    with stream:
        if (
            info.st_size != 65
            or stream.read(66) != hashlib.sha256(content).hexdigest().encode() + b"\n"
        ):
            raise BackupError("Incomplete backup or manifest integrity failure")
    required = {
        "format",
        "schema_revision",
        "application_ref",
        "postgres_major",
        "created_at",
        "storage_directories",
        "files",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != required
        or manifest["format"] != FORMAT
        or not isinstance(manifest["schema_revision"], str)
        or manifest["schema_revision"] not in REVISIONS
    ):
        raise BackupError("Unsupported backup manifest version or fields")
    _application_ref(manifest["application_ref"])
    if application_ref is not None and manifest["application_ref"] != application_ref:
        raise BackupError(
            "Application version mismatch; restore with the recorded application version"
        )
    if (
        type(manifest["postgres_major"]) is not int
        or not 14 <= manifest["postgres_major"] <= 99
    ):
        raise BackupError("Invalid PostgreSQL version")
    try:
        if datetime.fromisoformat(manifest["created_at"]).utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError):
        raise BackupError("Invalid backup timestamp") from None
    files, directories = manifest["files"], manifest["storage_directories"]
    if (
        not isinstance(files, list)
        or not 1 <= len(files) <= limits.max_files + 1
        or not isinstance(directories, list)
        or len(directories) > limits.max_files * 2 + 16
    ):
        raise BackupError("Invalid or oversized manifest inventory")
    declared = {}
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size_bytes", "sha256"}:
            raise BackupError("Invalid manifest file record")
        key = _key(item["path"])
        if key != "database.dump":
            if not key.startswith("storage/"):
                raise BackupError("Unexpected backup file")
            _key(key[8:], storage=True)
        maximum = (
            limits.max_dump_bytes if key == "database.dump" else limits.max_file_bytes
        )
        if (
            type(item["size_bytes"]) is not int
            or not 0 <= item["size_bytes"] <= maximum
            or not isinstance(item["sha256"], str)
            or not HEX.fullmatch(item["sha256"])
        ):
            raise BackupError("Invalid manifest size or hash")
        if key in declared:
            raise BackupError("Duplicate manifest path")
        declared[key] = item
        total += item["size_bytes"]
    if total > limits.max_total_bytes or "database.dump" not in declared:
        raise BackupError("Missing archive or excessive backup bytes")
    safe_dirs = [_key(value, storage=True) for value in directories]
    if len(set(safe_dirs)) != len(safe_dirs):
        raise BackupError("Duplicate directory entry")
    actual, actual_dirs = _inventory(root, limits, storage=False)
    expected_dirs = {"storage"} | {"storage/" + key for key in safe_dirs}
    if set(actual_dirs) != expected_dirs:
        raise BackupError("Backup directory inventory differs")
    actual_files = {item["path"]: item for item in actual}
    if set(actual_files) != set(declared) | {"manifest.json", "COMPLETE"}:
        raise BackupError("Backup file inventory differs")
    if any(actual_files[key] != value for key, value in declared.items()):
        raise BackupError("Backup bytes failed integrity verification")
    stream, _ = _open_file(root, "database.dump")
    with stream:
        if stream.read(5) != b"PGDMP":
            raise BackupError("Archive is not PostgreSQL custom format")
    return manifest


def restore_backup(
    *,
    database: Database,
    backup_root: Path,
    storage_root: Path,
    durable_parent: Path,
    application_ref: str,
    quiesced: bool = False,
    trust_backup: bool = False,
    tools: PgTools | None = None,
    limits: BackupLimits | None = None,
) -> dict:
    if quiesced is not True or trust_backup is not True:
        raise BackupError(
            "Restore requires explicit stopped-writers acknowledgement and trust in the backup SQL"
        )
    _application_ref(application_ref)
    limits, tools = limits or BackupLimits(), tools or PgTools()
    backup_root = _directory(backup_root)
    storage_root = _path(storage_root)
    if storage_root.is_relative_to(backup_root) or backup_root.is_relative_to(
        storage_root
    ):
        raise BackupError("Backup and restore trees must be disjoint")
    manifest = verify_backup(
        backup_root, application_ref=application_ref, limits=limits
    )
    major = database.empty_target_major()
    if major != manifest["postgres_major"]:
        raise BackupError("Restore PostgreSQL major version differs from backup")
    environment = database.environment()
    tools.check_versions(environment, major, limits)
    storage_root = _new_directory(storage_root, durable_parent)
    from backend.app.core.storage_barrier import storage_guard

    with storage_guard(storage_root):
        _write_new(
            storage_root / ".restore-incomplete",
            b"Do not start services. Restore has not completed.\n",
        )
        for directory in manifest["storage_directories"]:
            provision_directory(storage_root / directory, durable_parent=storage_root)
        for item in manifest["files"]:
            if item["path"].startswith("storage/"):
                _copy(backup_root, storage_root / item["path"][8:], item, storage_root)
        # Recheck all backup bytes and target emptiness immediately before SQL execution.
        verify_backup(backup_root, application_ref=application_ref, limits=limits)
        if database.empty_target_major() != major:
            raise BackupError("Restore target changed")
        dump_entry = next(
            item for item in manifest["files"] if item["path"] == "database.dump"
        )
        # A private, unlinked spool prevents a changed archive path from changing the
        # already-verified SQL stream. This costs up to max_dump_bytes extra disk.
        original, _ = _open_file(backup_root, "database.dump")
        with original, tempfile.TemporaryFile(dir=storage_root) as dump:
            _copy_stream(original, dump, dump_entry)
            dump.seek(0)
            tools.runner(
                tools.pg_restore
                + (
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    "--no-privileges",
                    "--no-tablespaces",
                    "--no-password",
                    "--dbname=" + database.database_name,
                ),
                env=environment,
                stdin=dump,
                stdout=io.BytesIO(),
                timeout=limits.timeout_seconds,
                max_output_bytes=1024 * 1024,
            )
        snapshot = database.source_snapshot(limits)
        if (
            snapshot["schema_revision"] != manifest["schema_revision"]
            or snapshot["postgres_major"] != major
        ):
            raise BackupError(
                "Restored database version differs; target remains incomplete"
            )
        entries = [
            {**item, "path": item["path"][8:]}
            for item in manifest["files"]
            if item["path"].startswith("storage/")
        ]
        for item in entries:
            if _hash_file(storage_root, item["path"], limits.max_file_bytes) != item:
                raise BackupError("Restored storage failed integrity verification")
        _validate_references(storage_root, entries, snapshot, limits)
        (storage_root / ".restore-incomplete").unlink()
        fsync_directory(storage_root)
        return manifest
