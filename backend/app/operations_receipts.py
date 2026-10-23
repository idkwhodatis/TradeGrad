"""Sanitized operator observations. These attest a past command, not readiness."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from backend.app.operations_schemas import BackupEvidence, BackupReceipt

NOTE = (
    "Operator-produced historical observations only; hashes establish integrity, not "
    "authenticity. Creation or verification does not prove restoration. Even a fresh "
    "restore observation does not certify the current deployment or current restore readiness."
)


def receipt_evidence(
    receipts, *, application_ref, schema_revision, now, max_age_seconds
):
    evidence = []
    for receipt in receipts:
        age = (now - receipt.observed_at).total_seconds()
        backup_age = (now - receipt.backup_created_at).total_seconds()
        if age < 0 or receipt.backup_created_at > receipt.observed_at:
            state = "invalid_time"
        elif not application_ref:
            state = "identity_unavailable"
        elif (
            receipt.application_ref != application_ref
            or receipt.schema_revision != schema_revision
        ):
            state = "version_mismatch"
        elif age > max_age_seconds or backup_age > max_age_seconds:
            state = "stale"
        else:
            state = "observed"
        evidence.append(
            BackupEvidence(
                receipt=receipt,
                state=state,
                age_seconds=max(0, age),
                backup_age_seconds=max(0, backup_age),
            )
        )
    return sorted(evidence, key=lambda item: item.receipt.observed_at, reverse=True)


def write_success_receipt(
    *, operation, manifest, backup_root, destination, limits=None
):
    """Called only after the requested command succeeds; never reads credentials.

    Exact manifest bytes are checked against COMPLETE and the successful operation's
    manifest. No storage path, database identifier, inventory or archive enters output.
    Existing output is never overwritten, so failed commands cannot replace evidence.
    """
    from backend.app.operations_backup import (
        BackupError,
        BackupLimits,
        _directory,
        _open_file,
        _path,
        _read_json,
    )

    root = _directory(backup_root)
    actual, content = _read_json(root, "manifest.json", limits or BackupLimits())
    digest = hashlib.sha256(content).hexdigest()
    marker, info = _open_file(root, "COMPLETE")
    with marker:
        if (
            actual != manifest
            or info.st_size != 65
            or marker.read(66) != digest.encode() + b"\n"
        ):
            raise BackupError(
                "Manifest changed after the operation; no receipt produced"
            )
    receipt = BackupReceipt(
        operation=operation,
        application_ref=manifest["application_ref"],
        schema_revision=manifest["schema_revision"],
        manifest_sha256=digest,
        backup_created_at=manifest["created_at"],
        observed_at=datetime.now(UTC),
    )
    destination = _path(Path(destination))
    # No private backup modification; receipt is separately provisioned metadata.
    if destination.is_relative_to(root):
        raise BackupError("Receipt output must be outside the private backup")
    parent = _directory(destination.parent)
    payload = (
        json.dumps(receipt.model_dump(mode="json"), sort_keys=True) + "\n"
    ).encode()
    # Pin the no-symlink parent descriptor; reject path replacement after writing.
    parent_fd = os.open(parent.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parent.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
            os.close(parent_fd)
            parent_fd = child
        fd = os.open(
            destination.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(fd, "wb") as stream:
            if stream.write(payload) != len(payload):
                raise BackupError("Receipt write was incomplete")
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent_fd)
        current, pinned = os.stat(parent, follow_symlinks=False), os.fstat(parent_fd)
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise BackupError("Receipt parent changed; output is not registered")
    finally:
        os.close(parent_fd)
    return receipt
