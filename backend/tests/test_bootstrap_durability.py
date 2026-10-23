"""Real temporary bytes with fake DB persistence; not PostgreSQL/power-loss proof.

Seed strategy source is read as inert text. No strategy, engine, subprocess,
network listener or container executes in these tests.
"""

import errno
import hashlib
import json
import os
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from backend.app import bootstrap
from backend.app.core import atomic_storage
from backend.app.models import DatasetVersion, User
from backend.app.services import DomainError


class FakeSession:
    """Apply only bootstrap's simple equality selects; no SQL/transaction claim."""

    def __init__(self, events=None):
        self.rows = []
        self.events = events if events is not None else []
        self.flushes = 0
        self.admissions = 0

    def connection(self):
        self.admissions += 1  # inert stand-in; real PG ordering is a separate gate
        return self

    def scalar(self, statement):
        model = statement.column_descriptions[0]["entity"]
        return next(
            (
                row
                for row in self.rows
                if isinstance(row, model)
                and all(
                    getattr(row, clause.left.key) == clause.right.value
                    for clause in statement._where_criteria
                )
            ),
            None,
        )

    def add(self, row):
        self.events.append(("add", row))
        self.rows.append(row)

    def flush(self):
        self.flushes += 1
        for row in self.rows:
            if row.id is None:
                row.id = uuid4()


@pytest.fixture
def fixture_inputs(tmp_path):
    root, source = tmp_path / "storage", tmp_path / "source"
    root.mkdir()
    source.mkdir()
    payloads = {
        "CN/INDEX.csv": b"date,close\n2024-01-02,100\n",
        "CN_list/list.csv": b"symbol\nINDEX\n",
        "US/INDEX.csv": b"date,close\n2024-01-02,200\n",
        "US_list/list.csv": b"symbol\nINDEX\n",
    }
    manifest = {
        "description": "Synthetic test input only",
        "source_commit": "a" * 40,
        "start": "2024-01-02",
        "end": "2024-01-02",
        "files": {},
    }
    for name, content in payloads.items():
        path = source / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        manifest["files"][name] = hashlib.sha256(content).hexdigest()
    (source / "manifest.json").write_text(json.dumps(manifest))
    return root, source, User(id=uuid4(), email="fixture@example.invalid")


def seed(session, inputs):
    root, source, user = inputs
    return bootstrap.seed_fixtures(session, user, root, source)


def test_every_file_is_durably_published_before_dataset_reference(
    fixture_inputs, monkeypatch
):
    root, _, _ = fixture_inputs
    events, descriptors = [], {}
    session = FakeSession(events)
    real_open, real_close = os.open, os.close
    real_fsync, real_link = os.fsync, os.link
    real_publish = bootstrap.publish_bytes

    def opened(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        descriptors[fd] = (
            descriptors[dir_fd] / path if dir_fd is not None else Path(path)
        )
        return fd

    def closed(fd):
        descriptors.pop(fd, None)
        real_close(fd)

    def synced(fd):
        real_fsync(fd)
        kind = "fsync_dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync_file"
        events.append((kind, descriptors[fd]))

    def linked(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
        real_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        events.append(("link", descriptors[dst_dir_fd] / dst))

    def published(target, content, *, sha256, durable_root):
        assert durable_root == root
        real_publish(target, content, sha256=sha256, durable_root=durable_root)
        events.append(("published", target))

    with monkeypatch.context() as patch:
        for name, function in (
            ("open", opened),
            ("close", closed),
            ("fsync", synced),
            ("link", linked),
        ):
            patch.setattr(atomic_storage.os, name, function)
        patch.setattr(bootstrap, "publish_bytes", published)
        datasets, strategies = seed(session, fixture_inputs)

    assert len(datasets) == 2 and len(strategies) == 4
    assert session.flushes == 1
    for dataset in datasets:
        target = root / dataset.storage_key
        manifest = target / "manifest.json"
        add_index = events.index(("add", dataset))
        assert events[add_index - 1] == ("published", manifest)
        assert events[add_index - 2] == ("fsync_dir", target)
        assert ("link", manifest) in events[:add_index]
        assert (
            hashlib.sha256(manifest.read_bytes()).hexdigest() == dataset.content_sha256
        )
        for name, digest in dataset.manifest["files"].items():
            path = target / name
            assert events.index(("published", path)) < events.index(("link", manifest))
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        for path in target.rglob("*"):
            if path.is_file():
                assert path.stat().st_mode & 0o222 == 0
    assert not list(root.rglob("*.tmp"))
    assert not list(root.rglob("*.staging"))


def test_duplicate_bootstrap_verifies_without_republishing_or_adding_rows(
    fixture_inputs, monkeypatch
):
    root, _, _ = fixture_inputs
    session = FakeSession()
    first = seed(session, fixture_inputs)
    before = {
        p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    }

    def forbidden(*args, **kwargs):
        pytest.fail("Duplicate bootstrap must verify, not rewrite immutable bytes")

    monkeypatch.setattr(bootstrap, "publish_bytes", forbidden)
    assert seed(session, fixture_inputs) == first
    assert len(session.rows) == 6
    assert before == {
        p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize(
    "member", ["manifest.json", "data/INDEX.csv", "universe/list.csv"]
)
@pytest.mark.parametrize("damage", ["hash", "missing", "directory", "symlink"])
def test_existing_immutable_fixture_is_never_silently_repaired(
    fixture_inputs, member, damage
):
    root, _, _ = fixture_inputs
    session = FakeSession()
    datasets, _ = seed(session, fixture_inputs)
    path = root / datasets[0].storage_key / member
    size = path.stat().st_size
    path.unlink()
    if damage == "hash":
        path.write_bytes(b"x" * size)
    elif damage == "directory":
        path.mkdir()
    elif damage == "symlink":
        path.symlink_to(root / datasets[1].storage_key / member)
    with pytest.raises((DomainError, OSError)):
        seed(session, fixture_inputs)
    assert len(session.rows) == 6
    assert session.flushes == 1
    if damage == "hash":
        assert path.read_bytes() == b"x" * size
    elif damage == "missing":
        assert not path.exists()
    elif damage == "directory":
        assert path.is_dir() and not list(path.iterdir())
    else:
        assert path.is_symlink()


@pytest.mark.parametrize("boundary", ["missing", "file", "symlink", "ancestor_symlink"])
def test_bootstrap_requires_explicit_existing_nonsymlink_storage_boundary(
    fixture_inputs, boundary, tmp_path
):
    root, source, user = fixture_inputs
    if boundary == "missing":
        root = tmp_path / "missing"
    elif boundary == "file":
        root = tmp_path / "not-directory"
        root.write_bytes(b"keep")
    elif boundary == "symlink":
        link = tmp_path / "link"
        link.symlink_to(root, target_is_directory=True)
        root = link
    else:
        link = tmp_path / "parent-link"
        link.symlink_to(tmp_path, target_is_directory=True)
        root = link / "storage"
    session = FakeSession()
    with pytest.raises(OSError):
        bootstrap.seed_fixtures(session, user, root, source)
    assert session.rows == [] and session.flushes == 0
    assert not list((tmp_path / "storage").iterdir())
    if boundary == "missing":
        assert not root.exists()


@pytest.mark.parametrize(
    "phase",
    ["partial_write", "file_fsync", "directory_fsync", "after_link", "manifest"],
)
def test_interrupted_publication_never_adds_a_dataset_and_retry_is_safe(
    fixture_inputs, monkeypatch, phase
):
    root, _, _ = fixture_inputs
    session = FakeSession()
    real_write, real_fsync, real_link = os.write, os.fsync, os.link
    real_publish = bootstrap.publish_bytes
    written = False
    linked = False

    def write(fd, content):
        nonlocal written
        if phase == "partial_write":
            if written:
                raise OSError(errno.ENOSPC, "injected partial write")
            written = True
            return real_write(fd, content[:1])
        return real_write(fd, content)

    def fsync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (
            (phase == "file_fsync" and not directory)
            or (phase == "directory_fsync" and directory)
            or (phase == "after_link" and directory and linked)
        ):
            raise OSError(errno.EIO, "injected fsync failure")
        return real_fsync(fd)

    def link(*args, **kwargs):
        nonlocal linked
        real_link(*args, **kwargs)
        linked = True

    def publish(target, content, **kwargs):
        if phase == "manifest" and target.name == "manifest.json":
            raise OSError(errno.ENOSPC, "injected manifest publication failure")
        real_publish(target, content, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(atomic_storage.os, "write", write)
        patch.setattr(atomic_storage.os, "fsync", fsync)
        patch.setattr(atomic_storage.os, "link", link)
        patch.setattr(bootstrap, "publish_bytes", publish)
        with pytest.raises(OSError, match="injected"):
            seed(session, fixture_inputs)
    assert session.rows == [] and session.flushes == 0
    assert not list(root.rglob("manifest.json"))
    assert not list(root.rglob("*.tmp"))
    orphans = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    datasets, _ = seed(session, fixture_inputs)
    assert len(datasets) == 2 and len(session.rows) == 6
    assert all(p.read_bytes() == content for p, content in orphans.items())
    # Every visible final file is complete; orphan cleanup is not bootstrap's job.
    assert all(len(content) > 1 for content in orphans.values())


def test_wrong_source_hash_never_publishes_or_adds_a_dataset(fixture_inputs):
    root, source, _ = fixture_inputs
    (source / "CN/INDEX.csv").write_bytes(b"corrupt source")
    session = FakeSession()
    with pytest.raises(DomainError, match="checksum"):
        seed(session, fixture_inputs)
    assert session.rows == [] and session.flushes == 0
    assert not list(root.rglob("*"))


def test_fake_database_flush_failure_leaves_only_complete_unreferenced_snapshots(
    fixture_inputs, monkeypatch
):
    root, _, _ = fixture_inputs
    session = FakeSession()

    def failed():
        raise RuntimeError("injected fake persistence failure")

    monkeypatch.setattr(session, "flush", failed)
    with pytest.raises(RuntimeError, match="fake persistence"):
        seed(session, fixture_inputs)
    snapshots = [row for row in session.rows if isinstance(row, DatasetVersion)]
    assert len(snapshots) == 2
    for row in snapshots:
        manifest = root / row.storage_key / "manifest.json"
        assert hashlib.sha256(manifest.read_bytes()).hexdigest() == row.content_sha256
    # Model a rollback by discarding the fake session. This is not a real commit
    # or rollback test; PostgreSQL boundaries remain an independent release gate.
    retry = FakeSession()
    datasets, _ = seed(retry, fixture_inputs)
    assert not {row.id for row in snapshots} & {row.id for row in datasets}
    assert all((root / row.storage_key / "manifest.json").exists() for row in snapshots)


@pytest.mark.parametrize(
    "member", ["data/INDEX.csv", "universe/list.csv", "manifest.json"]
)
@pytest.mark.parametrize("conflict", ["bytes", "directory"])
def test_new_publication_cannot_clobber_an_existing_final_path(
    fixture_inputs, monkeypatch, member, conflict
):
    root, _, user = fixture_inputs
    dataset_id = uuid4()
    monkeypatch.setattr(bootstrap, "uuid4", lambda: dataset_id)
    path = root / f"datasets/{user.id}/{dataset_id}" / member
    path.parent.mkdir(parents=True)
    if conflict == "bytes":
        path.write_bytes(b"preexisting conflict")
    else:
        path.mkdir()
    inode = path.stat().st_ino
    session = FakeSession()
    with pytest.raises((atomic_storage.ContentConflict, OSError)):
        seed(session, fixture_inputs)
    assert session.rows == [] and session.flushes == 0
    assert path.stat().st_ino == inode
    if conflict == "bytes":
        assert path.read_bytes() == b"preexisting conflict"
    else:
        assert path.is_dir() and not list(path.iterdir())
    assert not list(root.rglob("*.tmp"))


def test_duplicate_bootstrap_flushes_verified_legacy_tree_without_rewriting(
    fixture_inputs, monkeypatch
):
    root, _, _ = fixture_inputs
    session = FakeSession()
    datasets, _ = seed(session, fixture_inputs)
    verified, flushed = [], []
    real_read, real_sync = bootstrap.read_verified, bootstrap.sync_tree

    def read(storage, key, digest, maximum):
        content = real_read(storage, key, digest, maximum)
        verified.append(storage / key)
        return content

    def sync(target):
        dataset = next(row for row in datasets if root / row.storage_key == target)
        expected = {
            target / "manifest.json",
            *(target / name for name in dataset.manifest["files"]),
        }
        assert expected <= set(verified)
        real_sync(target)
        flushed.append(target)

    monkeypatch.setattr(bootstrap, "read_verified", read)
    monkeypatch.setattr(bootstrap, "sync_tree", sync)
    assert seed(session, fixture_inputs)[0] == datasets
    assert flushed == [root / row.storage_key for row in datasets]
    assert len(session.rows) == 6


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_duplicate_legacy_tree_sync_failure_is_not_reported_as_success(
    fixture_inputs, monkeypatch, kind
):
    root, _, _ = fixture_inputs
    session = FakeSession()
    seed(session, fixture_inputs)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    real_sync, real_fsync = bootstrap.sync_tree, os.fsync
    syncing = False

    def sync(target):
        nonlocal syncing
        syncing = True
        real_sync(target)

    def fsync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if syncing and directory == (kind == "directory"):
            raise OSError(errno.EIO, "injected legacy tree sync failure")
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "sync_tree", sync)
        patch.setattr(atomic_storage.os, "fsync", fsync)
        with pytest.raises(OSError, match="legacy tree sync"):
            seed(session, fixture_inputs)
    assert session.flushes == 1 and len(session.rows) == 6
    assert all(p.read_bytes() == content for p, content in before.items())


def test_direct_seed_admits_transaction_before_storage(fixture_inputs, monkeypatch):
    session = FakeSession()
    original = bootstrap.provision_directory

    def provision(*args, **kwargs):
        assert session.admissions == 1
        return original(*args, **kwargs)

    monkeypatch.setattr(bootstrap, "provision_directory", provision)
    seed(session, fixture_inputs)
