"""Storage publication tests use real files, concurrency and a real writer crash."""

import errno
import hashlib
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from backend.app.core import atomic_storage
from backend.app.core.atomic_storage import ContentConflict, publish_bytes


def test_concurrent_identical_publications_never_expose_partial_final(tmp_path):
    payload = b"complete-content" * 20000
    sha = hashlib.sha256(payload).hexdigest()
    target = tmp_path / sha
    barrier = Barrier(8)

    def publish():
        barrier.wait()
        publish_bytes(target, payload, sha256=sha)
        assert target.read_bytes() == payload

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: publish(), range(8)))
    assert target.stat().st_mode & 0o222 == 0
    assert list(tmp_path.iterdir()) == [target]


def test_partial_write_enospc_is_cleaned_and_identical_retry_succeeds(
    tmp_path, monkeypatch
):
    payload = b"abcdefgh" * 300
    sha = hashlib.sha256(payload).hexdigest()
    target = tmp_path / sha
    original = os.write
    calls = 0

    def interrupted(fd, content):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(fd, content[:5])
        raise OSError(errno.ENOSPC, "Injected full temporary disk")

    with monkeypatch.context() as patch:
        patch.setattr(atomic_storage.os, "write", interrupted)
        with pytest.raises(OSError, match="Injected"):
            publish_bytes(target, payload, sha256=sha)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
    publish_bytes(target, payload, sha256=sha)
    assert target.read_bytes() == payload


def test_real_writer_exit_before_publication_cannot_poison_identical_retry(tmp_path):
    target = tmp_path / "content"
    payload = b"complete immutable original source"
    sha = hashlib.sha256(payload).hexdigest()
    program = """
import os, sys
from pathlib import Path
from backend.app.core.atomic_storage import publish_bytes
original = os.write
def crash(fd, content):
    original(fd, content[:3])
    os._exit(75)
os.write = crash
publish_bytes(Path(sys.argv[1]), bytes.fromhex(sys.argv[2]), sha256=sys.argv[3])
"""
    process = subprocess.run(
        [sys.executable, "-c", program, str(target), payload.hex(), sha],
        cwd=Path(__file__).parents[2],
        check=False,
        timeout=10,
        capture_output=True,
    )
    assert process.returncode == 75
    assert not target.exists()
    assert len(list(tmp_path.glob(".*.tmp"))) == 1
    publish_bytes(target, payload, sha256=sha)
    assert target.read_bytes() == payload


def test_existing_conflicting_address_is_never_overwritten(tmp_path):
    target = tmp_path / "content"
    target.write_bytes(b"old corrupt bytes")
    before = target.stat()
    with pytest.raises(ContentConflict):
        publish_bytes(
            target, b"replacement", sha256=hashlib.sha256(b"replacement").hexdigest()
        )
    assert target.read_bytes() == b"old corrupt bytes"
    assert target.stat().st_ino == before.st_ino
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    assert list(tmp_path.iterdir()) == [target]


def test_existing_symlink_is_rejected_without_reading_or_mutating_destination(tmp_path):
    payload = b"safe"
    original = tmp_path / "original"
    original.write_bytes(payload)
    target = tmp_path / "content"
    target.symlink_to(original)
    with pytest.raises(OSError):
        publish_bytes(target, payload, sha256=hashlib.sha256(payload).hexdigest())
    assert original.read_bytes() == payload
    assert target.is_symlink()


def test_fsync_failure_before_publication_leaves_no_final_name(tmp_path, monkeypatch):
    target = tmp_path / "content"
    real = os.fsync
    calls = 0

    def fail_once(fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EIO, "Injected flush failure")
        real(fd)

    monkeypatch.setattr(atomic_storage.os, "fsync", fail_once)
    with pytest.raises(OSError, match="Injected"):
        publish_bytes(target, b"safe", sha256=hashlib.sha256(b"safe").hexdigest())
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_wrong_input_digest_does_not_write_any_object(tmp_path):
    with pytest.raises(ContentConflict):
        publish_bytes(tmp_path / "content", b"safe", sha256="0" * 64)
    assert list(tmp_path.iterdir()) == []
