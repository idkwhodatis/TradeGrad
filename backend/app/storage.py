"""Bounded private storage: resource IDs, immutable hashes and no symlink traversal."""

import hashlib
import os
import re
import stat
from pathlib import Path

from backend.app.services import DomainError

SENSITIVE_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[a-z0-9._~-]+"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)((?:password|secret|api[_-]?key|access[_-]?token|database_url)\s*[:=]\s*)[^\s,;]+"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"postgres(?:ql)?(?:\+psycopg)?://[^\s]+"), "[REDACTED_DATABASE_URL]"),
]


def redact(value: str) -> str:
    for pattern, replacement in SENSITIVE_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def safe_path(root: Path, key: str) -> Path:
    root = root.resolve()
    candidate = root / key
    if Path(key).is_absolute() or ".." in Path(key).parts:
        raise DomainError("unsafe_storage_path", "Invalid stored artifact path", 500)
    current = root
    for part in Path(key).parts:
        current = current / part
        if current.is_symlink():
            raise DomainError(
                "unsafe_storage_path", "Symlink storage entries are not allowed", 500
            )
    if not candidate.resolve().is_relative_to(root):
        raise DomainError(
            "unsafe_storage_path", "Artifact lies outside private storage", 500
        )
    return candidate


def read_verified(root: Path, key: str, expected_sha256: str, max_bytes: int) -> bytes:
    path = safe_path(root, key)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                raise DomainError(
                    "invalid_artifact", "Artifact is not a bounded regular file", 409
                )
            content = stream.read(max_bytes + 1)
    except FileNotFoundError:
        raise DomainError(
            "artifact_missing",
            "Artifact bytes are unavailable; restore the matching storage backup",
            409,
        ) from None
    if (
        len(content) > max_bytes
        or hashlib.sha256(content).hexdigest() != expected_sha256
    ):
        raise DomainError(
            "artifact_integrity",
            "Artifact content does not match its immutable checksum",
            409,
        )
    return content
