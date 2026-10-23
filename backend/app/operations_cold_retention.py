"""Explicit dry-run-first cold cleanup of verified, expired atomic temporaries.

This is not orphan GC. No published artifact, dataset, package, checkpoint,
source, replay journal or persistent Run log is ever a deletion candidate.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import text

from backend.app.core.atomic_storage import _absolute_path, _open_directory
from backend.app.core.maintenance import _lock
from backend.app.core.storage_barrier import storage_guard
from backend.app.operations_backup import BackupLimits, PostgresDatabase
from backend.app.operations_retention import (
    RUN_LOGS,
    TEMPORARIES,
    RetentionError,
    RetentionPolicy,
    _deadline,
    _fingerprint,
    _protected,
    _scan,
    _snapshot_manifest_keys,
)

FORMAT = "tradegrad.cold-retention.v1"
ACKNOWLEDGEMENT = "delete-only-expired-unreferenced-atomic-temporaries"


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


class LockedDatabase(PostgresDatabase):
    """Snapshot queries on the *same* exclusive-lock-holding transaction."""

    def __init__(self, source, connection):
        self.__dict__.update(source.__dict__)
        self.connection = connection
        self.transaction = connection.get_transaction()
        self.pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()

    def assert_locked(self):
        if (
            self.connection.closed
            or self.connection.invalidated
            or self.connection.get_transaction() is not self.transaction
            or not self.transaction.is_active
            or self.connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            != self.pid
        ):
            raise RetentionError("Maintenance connection changed; cleanup stopped")

    def _inspect(self, function):
        self.assert_locked()
        # Outbox is admission intent even if its Run/import currently looks terminal.
        for table in ("outbox_events", "data_outbox"):
            if self.connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE dispatched_at IS NULL")
            ).scalar_one():
                raise RetentionError(
                    "Pending dispatch must be resolved before cold cleanup"
                )
        if self.connection.execute(
            text(
                "SELECT count(*) FROM research_evaluations WHERE finished_at IS NULL "
                "OR status NOT IN ('succeeded','failed','cancelled','interrupted')"
            )
        ).scalar_one():
            raise RetentionError("Every evaluator needs a terminal completion receipt")
        # Model subprocesses can outlive a worker too. Preserve the same durable
        # admission/confirmed-stop fence as model_containment, not mere row status.
        rows = (
            self.connection.execute(
                text(
                    "SELECT attempt_id, type, payload FROM run_events "
                    "WHERE type IN ('model_process_admitted','model_process_stopped') "
                    "ORDER BY attempt_id, seq LIMIT 20001"
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > 20000:
            raise RetentionError("Model process stop inventory exceeds its limit")
        active, seen = set(), set()
        for row in rows:
            payload = row["payload"]
            if (
                row["attempt_id"] is None
                or type(payload) is not dict
                or set(payload) != {"call_id"}
                or not isinstance(payload["call_id"], str)
                or not 0 < len(payload["call_id"]) <= 160
            ):
                raise RetentionError("Unknown model process stop evidence")
            identity = (row["attempt_id"], payload["call_id"])
            if row["type"] == "model_process_admitted" and identity not in seen:
                active.add(identity)
                seen.add(identity)
            elif row["type"] == "model_process_stopped" and identity in active:
                active.remove(identity)
            else:
                raise RetentionError("Ambiguous model process stop evidence")
        if active:
            raise RetentionError("Unconfirmed model processes must be resolved first")
        return function(self.connection)


@contextmanager
def exclusive_maintenance(database: PostgresDatabase, storage_root: Path):
    """PG exclusive first, root exclusive second, both retained through receipt fsync."""
    engine = database._inspection_engine()
    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            _lock(connection, exclusive=True)
            with storage_guard(storage_root, exclusive=True) as fd:
                yield LockedDatabase(database, connection), fd
    finally:
        engine.dispose()


def _inventory(database, root, policy, limits, deadline, now):
    snapshot = database.source_snapshot(limits)
    keys = _snapshot_manifest_keys(snapshot, limits)
    first = _scan(root, keys, limits, deadline)
    protected, trees = _protected(snapshot, first[0], first[2], deadline)
    if snapshot != database.source_snapshot(limits) or first != _scan(
        root, keys, limits, deadline
    ):
        raise RetentionError("References or storage changed during cold inspection")
    cutoff = (
        int(now.timestamp() * 1_000_000_000)
        - policy.temporary_days * 86_400_000_000_000
    )
    log_cutoff = (
        int(now.timestamp() * 1_000_000_000) - policy.log_days * 86_400_000_000_000
    )
    candidates = [
        entry
        for key, entry in sorted(first[0].items())
        if TEMPORARIES.fullmatch(key)
        and max(entry["fingerprint"][-2:]) <= cutoff
        and key not in protected
        and not any(parent.as_posix() in trees for parent in PurePosixPath(key).parents)
    ]
    identity = {
        "format": FORMAT,
        "policy": vars(policy),
        "snapshot": snapshot,
        "files": first[0],
        "directories": first[1],
        "candidate_paths": [entry["path"] for entry in candidates],
    }
    report = {
        "format": FORMAT,
        "created_at": now.isoformat(),
        "plan_sha256": hashlib.sha256(_canonical(identity)).hexdigest(),
        "policy": vars(policy),
        "schema_revision": snapshot["schema_revision"],
        "files_scanned": len(first[0]),
        "bytes_scanned": sum(entry["size_bytes"] for entry in first[0].values()),
        "verified_reference_files": len(protected),
        "temporary_candidates": [
            {k: entry[k] for k in ("path", "sha256", "size_bytes")}
            for entry in candidates
        ],
        "candidate_bytes": sum(entry["size_bytes"] for entry in candidates),
        "aged_run_logs_retained": sum(
            bool(RUN_LOGS.fullmatch(key))
            and max(entry["fingerprint"][-2:]) <= log_cutoff
            for key, entry in first[0].items()
        ),
        "log_cleanup_supported": False,
        "deleted_files": 0,
        "retained_files": len(first[0]),
    }
    return report, snapshot, first, candidates


class Receipt:
    """Exclusive, private, fsynced JSONL journal outside the owned storage root.

    Intent is durable before unlink; completion is durable after directory fsync.
    A crash can leave an unresolved intent. Never infer rollback or retry blindly.
    """

    def __init__(self, path, root, max_bytes):
        self.path = _absolute_path(Path(path))
        if self.path.is_relative_to(root):
            raise RetentionError("Receipt must be outside the managed storage root")
        self.maximum = max_bytes
        self.written = 0
        self.parent = self.fd = None

    def __enter__(self):
        self.parent = _open_directory(self.path.parent)
        try:
            self.fd = os.open(
                self.path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.parent,
            )
            os.fsync(self.fd)
            os.fsync(self.parent)
            return self
        except BaseException:
            if self.fd is not None:
                os.close(self.fd)
            os.close(self.parent)
            raise

    def append(self, value):
        data = _canonical(value) + b"\n"
        if self.written + len(data) > self.maximum:
            raise RetentionError("Private cleanup receipt exceeds its byte budget")
        view = memoryview(data)
        while view:
            count = os.write(self.fd, view)
            if count <= 0:
                raise RetentionError("Private cleanup receipt write failed")
            view = view[count:]
        self.written += len(data)
        os.fsync(self.fd)

    def __exit__(self, *exception):
        os.close(self.fd)
        os.close(self.parent)


def _unlink_candidate(root, root_fd, directories, entry, deadline, database, receipt):
    """Pin/revalidate the exact ancestry and file bytes, then unlink relative to FD."""
    _deadline(deadline)
    current = _open_directory(root)
    try:
        if (
            _fingerprint(os.fstat(current)) != directories[""]
            or _fingerprint(os.fstat(root_fd)) != directories[""]
        ):
            raise RetentionError("Storage root identity changed")
    finally:
        os.close(current)
    parts = PurePosixPath(entry["path"]).parts
    parent = os.dup(root_fd)
    prefix = []
    try:
        for part in parts[:-1]:
            prefix.append(part)
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            os.close(parent)
            parent = child
            if _fingerprint(os.fstat(parent)) != directories["/".join(prefix)]:
                raise RetentionError("Candidate directory identity changed")
        fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or _fingerprint(before) != entry["fingerprint"]
            ):
                raise RetentionError("Candidate identity changed")
            digest, size = hashlib.sha256(), 0
            while chunk := os.read(
                fd, min(1024 * 1024, entry["size_bytes"] - size + 1)
            ):
                _deadline(deadline)
                digest.update(chunk)
                size += len(chunk)
                if size > entry["size_bytes"]:
                    raise RetentionError("Candidate size changed")
            if (
                size != entry["size_bytes"]
                or digest.hexdigest() != entry["sha256"]
                or _fingerprint(os.fstat(fd)) != entry["fingerprint"]
            ):
                raise RetentionError("Candidate bytes changed")
            receipt.append(
                {
                    "action": "unlink_intent",
                    "path": entry["path"],
                    "sha256": entry["sha256"],
                    "fingerprint": entry["fingerprint"],
                }
            )
            database.assert_locked()  # lost/reconnected PG never authorizes the next unlink
            # Detect ancestry rebinding during hashing/receipt fsync as well.
            check = _open_directory(root)
            chain = []
            try:
                if _fingerprint(os.fstat(check)) != directories[""]:
                    raise RetentionError("Storage root changed before unlink")
                for part in parts[:-1]:
                    chain.append(part)
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=check
                    )
                    os.close(check)
                    check = child
                    if _fingerprint(os.fstat(check)) != directories["/".join(chain)]:
                        raise RetentionError("Candidate ancestry changed before unlink")
                if _fingerprint(os.fstat(check)) != _fingerprint(os.fstat(parent)):
                    raise RetentionError("Candidate directory was replaced")
            finally:
                os.close(check)
            _deadline(deadline)
            if (
                _fingerprint(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False))
                != entry["fingerprint"]
            ):
                raise RetentionError("Candidate changed immediately before unlink")
            os.unlink(parts[-1], dir_fd=parent)
            os.fsync(parent)
            directories["/".join(prefix)] = _fingerprint(os.fstat(parent))
            receipt.append({"action": "unlinked", "path": entry["path"]})
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def cold_retention(
    *,
    database,
    storage_root,
    policy=None,
    limits=None,
    quiesced=False,
    apply=False,
    expected_plan_sha256=None,
    acknowledgement=None,
    receipt_path=None,
    now=None,
):
    """Default is locked dry-run. Apply requires exact fresh plan and acknowledgement."""
    if quiesced is not True:
        raise RetentionError(
            "Stop every writer and explicitly acknowledge cold quiescence"
        )
    if type(apply) is not bool:
        raise RetentionError("Apply must be explicit")
    if apply and (
        acknowledgement != ACKNOWLEDGEMENT
        or not isinstance(expected_plan_sha256, str)
        or len(expected_plan_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_plan_sha256)
        or receipt_path is None
    ):
        raise RetentionError(
            "Apply requires the dry-run plan hash, exact acknowledgement and new private receipt path"
        )
    policy, limits = policy or RetentionPolicy(), limits or BackupLimits()
    now = now or datetime.now(UTC)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise RetentionError("Retention time must include a timezone")
    root = _absolute_path(Path(storage_root))
    deadline = time.monotonic() + limits.timeout_seconds
    with exclusive_maintenance(database, root) as (locked, root_fd):
        report, snapshot, inventory, candidates = _inventory(
            locked, root, policy, limits, deadline, now
        )
        report.update(dry_run=not apply, status="planned" if not apply else "applying")
        if not apply:
            return report
        if report["plan_sha256"] != expected_plan_sha256:
            raise RetentionError(
                "Dry-run plan changed; inspect a new plan before applying"
            )
        if snapshot != locked.source_snapshot(limits):
            raise RetentionError("References changed before cleanup")
        # Reserve bounded receipt capacity up front, before any deletion.
        required = (
            len(_canonical(report))
            + sum(len(_canonical(entry)) * 3 + 512 for entry in candidates)
            + 4096
        )
        if required > limits.max_manifest_bytes:
            raise RetentionError("Planned cleanup receipt exceeds its byte budget")
        with Receipt(receipt_path, root, limits.max_manifest_bytes) as receipt:
            receipt.append({"action": "begin", "plan": report})
            try:
                for entry in candidates:
                    _unlink_candidate(
                        root, root_fd, inventory[1], entry, deadline, locked, receipt
                    )
                    report["deleted_files"] += 1
                    report["retained_files"] -= 1
                # All survivors, reference bytes and manifests must be identical.
                final = _scan(
                    root, _snapshot_manifest_keys(snapshot, limits), limits, deadline
                )
                deleted_paths = {entry["path"] for entry in candidates}
                remaining = {
                    key: value
                    for key, value in inventory[0].items()
                    if key not in deleted_paths
                }
                if (
                    final[0] != remaining
                    or final[1] != inventory[1]
                    or final[2] != inventory[2]
                    or locked.source_snapshot(limits) != snapshot
                ):
                    raise RetentionError(
                        "Unexpected storage or reference change after cleanup"
                    )
                _protected(snapshot, final[0], final[2], deadline)
                report["status"] = "applied"
                receipt.append({"action": "complete", "result": report})
            except BaseException:
                # Never mask the original failure if the receipt disk also failed.
                try:
                    receipt.append(
                        {
                            "action": "incomplete",
                            "confirmed_deleted_files": report["deleted_files"],
                            "message": "Inspect durable intents and storage; no rollback is implied",
                        }
                    )
                except Exception:  # noqa: BLE001, S110 -- preserve failure; never disclose private path/SQL
                    pass
                raise
        return report
