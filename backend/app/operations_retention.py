"""Legacy bounded read-only retention preview, never an apply authority.

The cold command uses the cooperative barrier in operations_cold_retention.
This helper remains a read-only inspection API for existing callers/tests and
cannot bypass that protocol, fresh-plan confirmation or durable receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from backend.app.core.atomic_storage import _absolute_path, _open_directory
from backend.app.operations_backup import (
    HEX,
    REVISIONS,
    BackupError,
    BackupLimits,
    _key,
)

FORMAT = "tradegrad.retention-preview.v1"
APPLY_BLOCKER = (
    "This advisory preview cannot apply: use the cold-maintenance exclusion protocol "
    "and freshly approved plan. Nothing was deleted."
)
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# These are literal layouts emitted by atomic_storage.publish_bytes callers.
# No generic *.tmp, .staging, recursive run, or suffix-only log deletion rule.
TEMPORARIES = re.compile(
    rf"(?:uploads/{UUID}/\.[a-f0-9]{{64}}\.(?:csv|parquet)"
    rf"|data-checkpoints/{UUID}/{UUID}/\.[a-f0-9]{{64}}\.json"
    rf"|strategy-packages/{UUID}/{UUID}/\.[a-f0-9]{{64}}\.zip"
    rf"|runs/{UUID}/{UUID}/\.execution-environment\.json"
    rf"|runs/{UUID}/{UUID}/evaluations/{UUID}/artifacts/\.(?:engine|policy)\.log)"
    r"\.[a-f0-9]{32}\.tmp\Z"
)
RUN_LOGS = re.compile(
    rf"runs/{UUID}/{UUID}/(?:artifacts/logs\.txt"
    rf"|evaluations/{UUID}/artifacts/(?:logs\.txt|engine\.log|policy\.log))\Z"
)


class RetentionError(ValueError):
    """Static, safe operator-facing failure; never include storage/DB contents."""


class SnapshotSource(Protocol):
    def source_snapshot(self, limits: BackupLimits) -> dict: ...


@dataclass(frozen=True)
class RetentionPolicy:
    temporary_days: int = 7
    log_days: int = 30

    def __post_init__(self):
        if any(type(v) is not int or not 1 <= v <= 36_500 for v in vars(self).values()):
            raise RetentionError("Retention ages must be whole days from 1 to 36500")


def _fingerprint(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _deadline(deadline):
    if time.monotonic() >= deadline:
        raise RetentionError("Preview time budget exceeded; no complete result")


def _snapshot_manifest_keys(snapshot: dict, limits: BackupLimits) -> set[str]:
    if (
        type(snapshot) is not dict
        or set(snapshot)
        != {
            "schema_revision",
            "postgres_major",
            "references",
            "datasets",
            "checkpoints",
        }
        or snapshot["schema_revision"] not in REVISIONS
        or type(snapshot["postgres_major"]) is not int
        or not 14 <= snapshot["postgres_major"] <= 99
    ):
        raise RetentionError("Unsupported database reference snapshot")
    rows = (snapshot["references"], snapshot["datasets"], snapshot["checkpoints"])
    if (
        any(type(value) is not list for value in rows)
        or sum(map(len, rows)) > limits.max_files
    ):
        raise RetentionError("Invalid or oversized database reference snapshot")
    keys = set()
    for group in rows:
        for row in group:
            if type(row) is not dict or not {"path", "sha256"} <= set(row):
                raise RetentionError("Unknown database reference shape")
            _key(row["path"], storage=True)
            if not isinstance(row["sha256"], str) or not HEX.fullmatch(row["sha256"]):
                raise RetentionError("Invalid immutable reference hash")
            if "size_bytes" in row and (
                type(row["size_bytes"]) is not int or row["size_bytes"] < 0
            ):
                raise RetentionError("Invalid immutable reference size")
    for item in snapshot["datasets"]:
        if type(item.get("manifest")) is not dict:
            raise RetentionError("Unknown dataset manifest shape")
        keys.add(_key(item["path"] + "/manifest.json", storage=True))
    keys.update(item["path"] for item in snapshot["checkpoints"])
    return keys


def _scan(root: Path, manifest_keys: set[str], limits: BackupLimits, deadline: float):
    """Read only through pinned, no-follow directory descriptors; bound all work."""
    files, directories, manifests = {}, {}, {}
    total_bytes = manifest_bytes = 0
    root_fd = _open_directory(root)
    root_info = _fingerprint(os.fstat(root_fd))

    def visit(fd, prefix, depth):
        nonlocal total_bytes, manifest_bytes
        _deadline(deadline)
        if depth > 32:
            raise RetentionError("Storage directory depth exceeds the preview limit")
        before = _fingerprint(os.fstat(fd))
        directories[prefix] = before
        with os.scandir(fd) as children:
            for child in children:
                _deadline(deadline)
                key = _key(
                    f"{prefix}/{child.name}" if prefix else child.name, storage=True
                )
                initial = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(initial.st_mode):
                    if len(directories) >= limits.max_files * 2 + 16:
                        raise RetentionError(
                            "Storage directory inventory exceeds the preview limit"
                        )
                    child_fd = os.open(
                        child.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fd,
                    )
                    try:
                        if _fingerprint(os.fstat(child_fd)) != _fingerprint(initial):
                            raise RetentionError("Storage changed during preview")
                        visit(child_fd, key, depth + 1)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(initial.st_mode) and initial.st_nlink == 1:
                    if (
                        len(files) >= limits.max_files
                        or initial.st_size > limits.max_file_bytes
                    ):
                        raise RetentionError(
                            "Storage file inventory exceeds the preview limit"
                        )
                    file_fd = os.open(
                        child.name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=fd,
                    )
                    try:
                        if _fingerprint(os.fstat(file_fd)) != _fingerprint(initial):
                            raise RetentionError("Storage changed during preview")
                        digest, size, content = hashlib.sha256(), 0, bytearray()
                        while chunk := os.read(
                            file_fd, min(1024 * 1024, limits.max_file_bytes - size + 1)
                        ):
                            _deadline(deadline)
                            size += len(chunk)
                            total_bytes += len(chunk)
                            if (
                                size > limits.max_file_bytes
                                or total_bytes > limits.max_total_bytes
                            ):
                                raise RetentionError(
                                    "Storage byte inventory exceeds the preview limit"
                                )
                            digest.update(chunk)
                            if key in manifest_keys:
                                manifest_bytes += len(chunk)
                                if manifest_bytes > limits.max_manifest_bytes:
                                    raise RetentionError(
                                        "Combined reference manifests exceed the preview limit"
                                    )
                                content.extend(chunk)
                        if size != initial.st_size or _fingerprint(
                            os.fstat(file_fd)
                        ) != _fingerprint(initial):
                            raise RetentionError("Storage changed during preview")
                        files[key] = {
                            "path": key,
                            "size_bytes": size,
                            "sha256": digest.hexdigest(),
                            "fingerprint": _fingerprint(initial),
                        }
                        if key in manifest_keys:
                            manifests[key] = bytes(content)
                    finally:
                        os.close(file_fd)
                else:
                    raise RetentionError(
                        "Symlinks, hard links and special storage files are forbidden"
                    )
                if _fingerprint(
                    os.stat(child.name, dir_fd=fd, follow_symlinks=False)
                ) != _fingerprint(initial):
                    raise RetentionError("Storage changed during preview")
        if _fingerprint(os.fstat(fd)) != before:
            raise RetentionError("Storage changed during preview")

    try:
        visit(root_fd, "", 0)
        current_fd = _open_directory(root)
        try:
            if _fingerprint(os.fstat(current_fd)) != root_info:
                raise RetentionError("Storage root changed during preview")
        finally:
            os.close(current_fd)
    finally:
        os.close(root_fd)
    return files, directories, manifests


def _json_manifest(content):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RetentionError("Duplicate reference manifest field")
            result[key] = value
        return result

    try:
        value = json.loads(content, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise RetentionError("Invalid reference manifest") from None
    if type(value) is not dict:
        raise RetentionError("Invalid reference manifest")
    return value


def _protected(snapshot, files, manifests, deadline):
    """Same reference classes as cold backup, verified against pinned read bytes.

    Retain whole dataset/checkpoint subtrees, not merely enumerated files. All
    other non-temporary objects are retained even when they have no DB pointer.
    """
    exact, trees = set(), set()

    def check(key, sha, size=None):
        _deadline(deadline)
        _key(key, storage=True)
        entry = files.get(key)
        if (
            not isinstance(sha, str)
            or not HEX.fullmatch(sha)
            or (size is not None and (type(size) is not int or size < 0))
            or entry is None
            or entry["sha256"] != sha
            or (size is not None and entry["size_bytes"] != size)
        ):
            raise RetentionError(
                "Reference bytes are missing or differ from their immutable hash"
            )
        exact.add(key)

    for item in snapshot["references"]:
        check(item["path"], item["sha256"], item.get("size_bytes"))
    for item in snapshot["datasets"]:
        base = item["path"]
        trees.add(base)
        key = base + "/manifest.json"
        check(key, item["sha256"])
        manifest = _json_manifest(manifests[key])
        if manifest != item["manifest"]:
            raise RetentionError("Dataset manifest differs from its database reference")
        for field in ("files", "artifact_files"):
            mapping = manifest.get(field, {})
            if type(mapping) is not dict:
                raise RetentionError("Unknown dataset file inventory")
            for name, sha in mapping.items():
                check(f"{base}/{_key(name)}", sha)
        previews = manifest.get("preview_files", {})
        if type(previews) is not dict:
            raise RetentionError("Unknown dataset preview inventory")
        for group in previews.values():
            _deadline(deadline)
            if type(group) is not list:
                raise RetentionError("Unknown dataset preview inventory")
            for entry in group:
                if type(entry) is not dict or not {"path", "sha256"} <= set(entry):
                    raise RetentionError("Unknown dataset preview reference")
                check(f"{base}/{_key(entry['path'])}", entry["sha256"])
    for item in snapshot["checkpoints"]:
        check(item["path"], item["sha256"])
        base = PurePosixPath(item["path"]).parent.as_posix()
        trees.add(base)
        manifest = _json_manifest(manifests[item["path"]])
        if type(manifest.get("files")) is not list:
            raise RetentionError("Unknown checkpoint file inventory")
        for entry in manifest["files"]:
            if type(entry) is not dict or not {"path", "sha256", "size_bytes"} <= set(
                entry
            ):
                raise RetentionError("Unknown checkpoint file reference")
            if type(entry["size_bytes"]) is not int or entry["size_bytes"] < 0:
                raise RetentionError("Invalid checkpoint file size")
            check(f"{base}/{_key(entry['path'])}", entry["sha256"], entry["size_bytes"])
    return exact, trees


def preview_retention(
    *,
    database: SnapshotSource,
    storage_root: Path,
    policy: RetentionPolicy | None = None,
    limits: BackupLimits | None = None,
    quiesced: bool = False,
    apply: bool = False,
    now: datetime | None = None,
) -> dict:
    """Return advisory metadata only. No unlink, rename, chmod, write or DB mutation."""
    if apply is not False:
        raise RetentionError(APPLY_BLOCKER)
    if quiesced is not True:
        raise RetentionError(
            "Preview requires explicit acknowledgement that every writer is stopped"
        )
    policy = policy or RetentionPolicy()
    limits = limits or BackupLimits()
    now = now or datetime.now(UTC)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise RetentionError("Preview time must include a timezone")
    deadline = time.monotonic() + limits.timeout_seconds
    try:
        root = _absolute_path(Path(storage_root))
        # The production SnapshotSource is PostgresDatabase.source_snapshot:
        # supported schema, stopped attempts/evaluators, no imports/queue leases.
        snapshot = database.source_snapshot(limits)
        manifest_keys = _snapshot_manifest_keys(snapshot, limits)
        first = _scan(root, manifest_keys, limits, deadline)
        protected, trees = _protected(snapshot, first[0], first[2], deadline)
        second_snapshot = database.source_snapshot(limits)
        _snapshot_manifest_keys(second_snapshot, limits)
        if snapshot != second_snapshot:
            raise RetentionError("Database references changed during preview")
        second = _scan(root, manifest_keys, limits, deadline)
        if first != second:
            raise RetentionError("Storage changed during preview")
        _deadline(deadline)
    except RetentionError:
        raise
    except (BackupError, OSError, ValueError, TypeError, KeyError, RecursionError):
        raise RetentionError(
            "Cold inspection failed; no complete retention preview"
        ) from None

    now_ns = int(now.timestamp() * 1_000_000_000)
    temp_cutoff = now_ns - policy.temporary_days * 86_400 * 1_000_000_000
    log_cutoff = now_ns - policy.log_days * 86_400 * 1_000_000_000
    candidates, old_logs = [], 0
    for key, entry in sorted(first[0].items()):
        _deadline(deadline)
        changed_ns = max(entry["fingerprint"][-2:])
        if RUN_LOGS.fullmatch(key) and changed_ns <= log_cutoff:
            old_logs += 1
        if (
            TEMPORARIES.fullmatch(key)
            and changed_ns <= temp_cutoff
            and key not in protected
            and not any(
                parent.as_posix() in trees for parent in PurePosixPath(key).parents
            )
        ):
            candidates.append({k: entry[k] for k in ("path", "size_bytes", "sha256")})
    return {
        "format": FORMAT,
        "status": "preview_only",
        "dry_run": True,
        "apply_supported": False,
        "apply_blocker": APPLY_BLOCKER,
        "created_at": now.isoformat(),
        "policy": vars(policy),
        "schema_revision": snapshot["schema_revision"],
        "files_scanned": len(first[0]),
        "bytes_scanned": sum(entry["size_bytes"] for entry in first[0].values()),
        "verified_reference_files": len(protected),
        "temporary_review_candidates": candidates,
        "temporary_review_bytes": sum(entry["size_bytes"] for entry in candidates),
        "aged_run_logs_retained": old_logs,
        "log_cleanup_supported": False,
        "deleted_files": 0,
        "retained_files": len(first[0]),
    }
