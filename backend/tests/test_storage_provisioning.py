"""Directory flush contracts, not power-loss or filesystem certification."""

import pytest

from backend.app.core.atomic_storage import ContentConflict, publish_bytes
from script.provision_storage import main


def test_cli_requires_an_explicit_durable_boundary(tmp_path):
    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path / "storage")])
    assert not (tmp_path / "storage").exists()


def test_cli_provisions_nested_root_and_supports_idempotent_publication(tmp_path):
    from hashlib import sha256

    root = tmp_path / "var" / "storage"
    args = ["--root", str(root), "--durable-parent", str(tmp_path)]
    main(args)
    main(args)
    target = root / "owner" / "artifact" / "result.json"
    content = b'{"fixture":true}'
    publish_bytes(
        target, content, sha256=sha256(content).hexdigest(), durable_root=root
    )
    assert target.read_bytes() == content


def test_cli_does_not_create_an_unspecified_or_missing_boundary(tmp_path):
    boundary = tmp_path / "not-provisioned"
    with pytest.raises((OSError, ContentConflict)):
        main(["--root", str(boundary / "storage"), "--durable-parent", str(boundary)])
    assert not boundary.exists()


def test_cli_rejects_escape_and_symlinked_root(tmp_path):
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    outside = tmp_path / "outside"
    with pytest.raises(ContentConflict):
        main(["--root", str(outside), "--durable-parent", str(boundary)])
    assert not outside.exists()
    link = boundary / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((OSError, ContentConflict)):
        main(["--root", str(link / "escaped"), "--durable-parent", str(boundary)])
    assert not (tmp_path / "escaped").exists()
