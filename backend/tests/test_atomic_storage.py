"""Storage publication tests use real files, concurrency and a real writer crash."""

import errno
import hashlib
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest

from backend.app.core import atomic_storage
from backend.app.core.atomic_storage import (
    ContentConflict,
    provision_directory,
    publish_bytes,
)


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


@pytest.mark.parametrize(
    ("crash_at", "exit_code", "published", "temporary_count"),
    [
        ("mkdir", 75, False, 0),
        ("write", 76, False, 1),
        ("link", 77, True, 1),
        ("directory_fsync", 78, True, 0),
    ],
)
def test_real_writer_exit_cannot_poison_identical_retry(
    tmp_path, crash_at, exit_code, published, temporary_count
):
    target = tmp_path / "packages" / "project" / "content"
    payload = b"complete immutable original source"
    sha = hashlib.sha256(payload).hexdigest()
    # Every injected exit is inside the subprocess this test creates. These are
    # process-interruption tests, not storage-device/power-loss simulations.
    program = """
import os, stat, sys
from pathlib import Path
from backend.app.core.atomic_storage import publish_bytes
target, root = Path(sys.argv[1]), Path(sys.argv[4])
phase = sys.argv[5]
original_mkdir, original_write = os.mkdir, os.write
original_link, original_fsync = os.link, os.fsync
def mkdir(*args, **kwargs):
    original_mkdir(*args, **kwargs)
    if phase == "mkdir":
        os._exit(75)
def write(fd, content):
    if phase == "write":
        original_write(fd, content[:3])
        os._exit(76)
    return original_write(fd, content)
def link(*args, **kwargs):
    original_link(*args, **kwargs)
    if phase == "link":
        os._exit(77)
def fsync(fd):
    original_fsync(fd)
    if phase == "directory_fsync" and stat.S_ISDIR(os.fstat(fd).st_mode):
        if target.exists():
            os._exit(78)
os.mkdir, os.write, os.link, os.fsync = mkdir, write, link, fsync
publish_bytes(target, bytes.fromhex(sys.argv[2]), sha256=sys.argv[3], durable_root=root)
"""
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(target),
            payload.hex(),
            sha,
            str(tmp_path),
            crash_at,
        ],
        cwd=Path(__file__).parents[2],
        check=False,
        timeout=10,
        capture_output=True,
    )
    assert process.returncode == exit_code, process.stderr.decode()
    assert target.exists() is published
    if published:
        assert target.read_bytes() == payload
    assert len(list(tmp_path.rglob(".*.tmp"))) == temporary_count
    publish_bytes(target, payload, sha256=sha, durable_root=tmp_path)
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

    def fail_once(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "Injected file flush failure")
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


def _record_storage_syscalls(monkeypatch):
    """Record completed calls and live descriptors without relying on /proc."""
    events = []
    descriptors = {}
    real_open, real_close, real_mkdir = os.open, os.close, os.mkdir
    real_fsync, real_link, real_unlink = os.fsync, os.link, os.unlink

    def absolute(path, dir_fd=None):
        return (descriptors[dir_fd] / path) if dir_fd is not None else Path(path)

    def opened(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        descriptors[fd] = absolute(path, dir_fd)
        if flags & os.O_CREAT:
            events.append(("create", descriptors[fd]))
        return fd

    def closed(fd):
        # Remove bookkeeping before close makes this number reusable by a peer.
        descriptors.pop(fd)
        real_close(fd)

    def mkdir(path, mode=0o777, *, dir_fd=None):
        try:
            real_mkdir(path, mode, dir_fd=dir_fd)
        except FileExistsError:
            events.append(("mkdir_exists", absolute(path, dir_fd)))
            raise
        events.append(("mkdir", absolute(path, dir_fd)))

    def fsync(fd):
        real_fsync(fd)
        kind = "fsync_dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync_file"
        events.append((kind, descriptors[fd]))

    def link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
        real_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        events.append(("link", absolute(dst, dst_dir_fd)))

    def unlink(path, *, dir_fd=None):
        real_unlink(path, dir_fd=dir_fd)
        events.append(("unlink", absolute(path, dir_fd)))

    for name, function in (
        ("open", opened),
        ("close", closed),
        ("mkdir", mkdir),
        ("fsync", fsync),
        ("link", link),
        ("unlink", unlink),
    ):
        monkeypatch.setattr(atomic_storage.os, name, function)
    return events, descriptors


@pytest.mark.parametrize("preexisting", [False, True])
def test_nested_entries_are_flushed_parent_to_child_before_file_creation(
    tmp_path, monkeypatch, preexisting
):
    root = tmp_path / "storage"
    root.mkdir()
    directories = [root, root / "packages", root / "packages" / "project"]
    target = directories[-1] / "content"
    if preexisting:
        target.parent.mkdir(parents=True)
    with monkeypatch.context() as patch:
        events, descriptors = _record_storage_syscalls(patch)
        publish_bytes(
            target,
            b"safe",
            sha256=hashlib.sha256(b"safe").hexdigest(),
            durable_root=root,
        )
    temporary = next(path for kind, path in events if kind == "create")
    mkdir = "mkdir_exists" if preexisting else "mkdir"
    assert events == [
        (mkdir, directories[1]),
        ("fsync_dir", root),
        (mkdir, directories[2]),
        ("fsync_dir", directories[1]),
        ("fsync_dir", directories[2]),
        ("create", temporary),
        ("fsync_file", temporary),
        ("link", target),
        ("unlink", temporary),
        ("fsync_dir", directories[2]),
    ]
    assert descriptors == {}
    assert target.read_bytes() == b"safe"


@pytest.mark.parametrize("failed_flush", [1, 2, 3, 4])
def test_each_directory_flush_failure_aborts_before_any_file_and_retry_is_safe(
    tmp_path, monkeypatch, failed_flush
):
    target = tmp_path / "packages" / "owner" / "hash" / "content"
    with monkeypatch.context() as patch:
        events, descriptors = _record_storage_syscalls(patch)
        recorded_fsync = os.fsync
        calls = 0

        def fail(fd):
            nonlocal calls
            calls += 1
            if calls == failed_flush:
                raise OSError(errno.EIO, "Injected directory flush failure")
            recorded_fsync(fd)

        patch.setattr(atomic_storage.os, "fsync", fail)
        with pytest.raises(OSError, match="Injected directory"):
            publish_bytes(
                target,
                b"safe",
                sha256=hashlib.sha256(b"safe").hexdigest(),
                durable_root=tmp_path,
            )
    assert not any(kind in {"create", "link"} for kind, _ in events)
    assert descriptors == {}
    assert not target.exists()
    assert list(tmp_path.rglob(".*.tmp")) == []
    publish_bytes(
        target,
        b"safe",
        sha256=hashlib.sha256(b"safe").hexdigest(),
        durable_root=tmp_path,
    )
    assert target.read_bytes() == b"safe"


def test_post_link_directory_flush_failure_is_reported_and_identical_retry_succeeds(
    tmp_path, monkeypatch
):
    target = tmp_path / "packages" / "content"
    real_fsync = os.fsync

    def fail(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and target.exists():
            raise OSError(errno.EIO, "Injected post-link directory flush failure")
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(atomic_storage.os, "fsync", fail)
        with pytest.raises(OSError, match="Injected post-link"):
            publish_bytes(
                target,
                b"safe",
                sha256=hashlib.sha256(b"safe").hexdigest(),
                durable_root=tmp_path,
            )
    assert target.read_bytes() == b"safe"
    assert list(tmp_path.rglob(".*.tmp")) == []
    publish_bytes(
        target,
        b"safe",
        sha256=hashlib.sha256(b"safe").hexdigest(),
        durable_root=tmp_path,
    )
    assert target.read_bytes() == b"safe"


def test_concurrent_nested_publications_share_explicit_boundary(tmp_path):
    target = tmp_path / "packages" / "owner" / "hash" / "content"
    payload = b"complete-content" * 20000
    sha = hashlib.sha256(payload).hexdigest()
    barrier = Barrier(8)

    def publish():
        barrier.wait()
        publish_bytes(target, payload, sha256=sha, durable_root=tmp_path)
        assert target.read_bytes() == payload

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: publish(), range(8)))
    assert list(target.parent.iterdir()) == [target]
    assert target.stat().st_mode & 0o222 == 0


def test_second_publisher_flushes_entry_created_by_unfinished_first_publisher(
    tmp_path, monkeypatch
):
    target = tmp_path / "packages" / "owner" / "content"
    payload = b"safe"
    sha = hashlib.sha256(payload).hexdigest()
    created, release = Event(), Event()
    with monkeypatch.context() as patch:
        events, descriptors = _record_storage_syscalls(patch)
        recorded_mkdir = os.mkdir

        def paused_creator(path, mode=0o777, *, dir_fd=None):
            recorded_mkdir(path, mode, dir_fd=dir_fd)
            if path == "packages":
                created.set()
                if not release.wait(10):
                    raise RuntimeError("Test did not release its paused publisher")
                raise OSError(errno.EIO, "First publisher stopped before parent fsync")

        patch.setattr(atomic_storage.os, "mkdir", paused_creator)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                publish_bytes, target, payload, sha256=sha, durable_root=tmp_path
            )
            try:
                assert created.wait(10)
                second = pool.submit(
                    publish_bytes, target, payload, sha256=sha, durable_root=tmp_path
                )
                second.result(timeout=10)
                assert target.read_bytes() == payload
            finally:
                release.set()
            with pytest.raises(OSError, match="First publisher stopped"):
                first.result(timeout=10)
    assert ("mkdir_exists", tmp_path / "packages") in events
    assert events.index(("fsync_dir", tmp_path)) < events.index(("link", target))
    assert descriptors == {}


@pytest.mark.parametrize("symlink_at", ["root", "root_ancestor", "branch", "leaf"])
def test_directory_symlinks_are_rejected_without_writing_outside(tmp_path, symlink_at):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "storage"
    target = root / "packages" / "owner" / "content"
    if symlink_at == "root":
        root.symlink_to(outside, target_is_directory=True)
    elif symlink_at == "root_ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        root.mkdir()
        root = alias / "storage"
        target = root / "packages" / "owner" / "content"
    else:
        root.mkdir()
        branch = root / "packages"
        if symlink_at == "leaf":
            branch.mkdir()
            branch = branch / "owner"
        branch.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        publish_bytes(
            target,
            b"safe",
            sha256=hashlib.sha256(b"safe").hexdigest(),
            durable_root=root,
        )
    assert list(outside.iterdir()) == []
    assert not target.exists()


def test_concurrent_mkdir_symlink_is_not_treated_as_a_directory(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    real_mkdir = os.mkdir

    def substitute_symlink(path, mode=0o777, *, dir_fd=None):
        if path == "packages":
            os.symlink(outside, path, dir_fd=dir_fd, target_is_directory=True)
            raise FileExistsError(errno.EEXIST, "Concurrent symlink")
        real_mkdir(path, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(atomic_storage.os, "mkdir", substitute_symlink)
        with pytest.raises(OSError):
            publish_bytes(
                tmp_path / "packages" / "content",
                b"safe",
                sha256=hashlib.sha256(b"safe").hexdigest(),
                durable_root=tmp_path,
            )
    assert list(outside.iterdir()) == []


def test_missing_explicit_boundary_is_not_silently_assumed_durable(tmp_path):
    root = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        publish_bytes(
            root / "packages" / "content",
            b"safe",
            sha256=hashlib.sha256(b"safe").hexdigest(),
            durable_root=root,
        )
    assert list(tmp_path.iterdir()) == []


def test_target_outside_boundary_is_rejected_without_creating_directories(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    with pytest.raises(ContentConflict, match="outside"):
        publish_bytes(
            tmp_path / "elsewhere" / "content",
            b"safe",
            sha256=hashlib.sha256(b"safe").hexdigest(),
            durable_root=root,
        )
    assert list(tmp_path.iterdir()) == [root]


@pytest.mark.parametrize("suffix", ["nested/../content", "nested/.."])
def test_parent_traversal_is_rejected_before_creating_directories(tmp_path, suffix):
    with pytest.raises(ContentConflict, match="parent traversal"):
        publish_bytes(
            tmp_path / suffix,
            b"safe",
            sha256=hashlib.sha256(b"safe").hexdigest(),
            durable_root=tmp_path,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("preexisting", [False, True])
def test_provision_directory_flushes_only_declared_hierarchy(
    tmp_path, monkeypatch, preexisting
):
    root = tmp_path / "private" / "storage"
    if preexisting:
        root.mkdir(parents=True)
    with monkeypatch.context() as patch:
        events, descriptors = _record_storage_syscalls(patch)
        provision_directory(root, durable_parent=tmp_path)
    mkdir = "mkdir_exists" if preexisting else "mkdir"
    assert events == [
        (mkdir, tmp_path / "private"),
        ("fsync_dir", tmp_path),
        (mkdir, root),
        ("fsync_dir", tmp_path / "private"),
        ("fsync_dir", root),
    ]
    assert descriptors == {}
    assert root.is_dir()
    publish_bytes(
        root / "packages" / "content",
        b"safe",
        sha256=hashlib.sha256(b"safe").hexdigest(),
        durable_root=root,
    )


@pytest.mark.parametrize("invalid_boundary", ["missing", "outside", "symlink"])
def test_provision_directory_requires_existing_real_containing_boundary(
    tmp_path, invalid_boundary
):
    existing = tmp_path / "existing"
    existing.mkdir()
    if invalid_boundary == "missing":
        boundary = tmp_path / "absent"
        target = boundary / "storage"
        error = FileNotFoundError
    elif invalid_boundary == "outside":
        boundary = existing
        target = tmp_path / "storage"
        error = ContentConflict
    else:
        boundary = tmp_path / "alias"
        boundary.symlink_to(existing, target_is_directory=True)
        target = boundary / "storage"
        error = OSError
    with pytest.raises(error):
        provision_directory(target, durable_parent=boundary)
    assert list(existing.iterdir()) == []
    assert not target.exists()


def test_provision_directory_flush_failure_prevents_success_and_allows_retry(
    tmp_path, monkeypatch
):
    root = tmp_path / "storage"
    real_fsync = os.fsync
    calls = 0

    def fail_leaf(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "Injected provision leaf flush failure")
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(atomic_storage.os, "fsync", fail_leaf)
        with pytest.raises(OSError, match="Injected provision"):
            provision_directory(root, durable_parent=tmp_path)
    provision_directory(root, durable_parent=tmp_path)
    assert root.is_dir()


@pytest.mark.parametrize("failure_at", ["create", "link"])
def test_temp_creation_or_link_failure_does_not_publish_and_closes_descriptors(
    tmp_path, monkeypatch, failure_at
):
    target = tmp_path / "packages" / "content"
    with monkeypatch.context() as patch:
        events, descriptors = _record_storage_syscalls(patch)
        recorded_open = os.open

        def fail_open(path, flags, mode=0o777, *, dir_fd=None):
            if flags & os.O_CREAT:
                raise OSError(errno.ENOSPC, "Injected temporary create failure")
            return recorded_open(path, flags, mode, dir_fd=dir_fd)

        def fail_link(*args, **kwargs):
            raise OSError(errno.EIO, "Injected publication link failure")

        if failure_at == "create":
            patch.setattr(atomic_storage.os, "open", fail_open)
        else:
            patch.setattr(atomic_storage.os, "link", fail_link)
        with pytest.raises(OSError, match="Injected"):
            publish_bytes(
                target,
                b"safe",
                sha256=hashlib.sha256(b"safe").hexdigest(),
                durable_root=tmp_path,
            )
    assert descriptors == {}
    assert not target.exists()
    assert list(target.parent.iterdir()) == []
    assert not any(kind == "link" for kind, _ in events)


def test_existing_same_size_wrong_hash_is_not_replaced(tmp_path):
    target = tmp_path / "content"
    target.write_bytes(b"evil")
    before = target.stat()
    with pytest.raises(ContentConflict, match="invalid hash"):
        publish_bytes(
            target,
            b"safe",
            sha256=hashlib.sha256(b"safe").hexdigest(),
            durable_root=tmp_path,
        )
    assert target.read_bytes() == b"evil"
    assert target.stat().st_ino == before.st_ino
    assert target.stat().st_mtime_ns == before.st_mtime_ns
