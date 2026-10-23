"""Receipt success/failure contracts with synthetic files, not a restore drill."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.app.operations_backup import BackupError
from backend.app.operations_receipts import receipt_evidence, write_success_receipt
from backend.app.operations_schemas import BackupReceipt

NOW = datetime(2026, 10, 23, tzinfo=UTC)
APP = "a" * 40


def receipt(**changes):
    return BackupReceipt(
        operation="create",
        application_ref=APP,
        schema_revision="005_settings",
        manifest_sha256="b" * 64,
        backup_created_at=NOW - timedelta(seconds=5),
        observed_at=NOW,
        **changes,
    )


def evidence(value, **changes):
    options = {
        "application_ref": APP,
        "schema_revision": "005_settings",
        "now": NOW,
        "max_age_seconds": 60,
    }
    options.update(changes)
    return receipt_evidence([value], **options)[0]


def test_receipt_states_never_invent_restore_or_readiness():
    value = receipt()
    assert evidence(value).state == "observed"
    assert evidence(value, now=NOW + timedelta(seconds=61)).state == "stale"
    assert evidence(value, application_ref="c" * 40).state == "version_mismatch"
    assert evidence(value, schema_revision="004_m5").state == "version_mismatch"
    assert evidence(value, application_ref=None).state == "identity_unavailable"
    assert evidence(value, now=NOW - timedelta(seconds=1)).state == "invalid_time"
    fresh_verify_old_backup = value.model_copy(
        update={"operation": "verify", "backup_created_at": NOW - timedelta(days=10)}
    )
    assert evidence(fresh_verify_old_backup).state == "stale"
    assert (
        value.operation == "create" and "ready" not in evidence(value).model_dump_json()
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("private_path", "/backup"),
        ("schema_revision", "head"),
        ("manifest_sha256", "not-a-hash"),
        ("application_ref", "latest"),
        ("observed_at", "2026-10-23T00:00:00"),
    ],
)
def test_strict_receipt_rejects_paths_unknown_revisions_and_naive_times(field, value):
    body = receipt().model_dump(mode="json")
    body[field] = value
    with pytest.raises(ValidationError):
        BackupReceipt.model_validate(body)


def synthetic_manifest(tmp_path):
    root = tmp_path / "backup"
    root.mkdir()
    manifest = {
        "application_ref": APP,
        "schema_revision": "005_settings",
        "created_at": NOW.isoformat(),
        "private_inventory": "not-in-receipt",
    }
    content = json.dumps(
        manifest, indent=2
    ).encode()  # exact bytes, not canonical reserialization
    (root / "manifest.json").write_bytes(content)
    (root / "COMPLETE").write_text(hashlib.sha256(content).hexdigest() + "\n")
    return root, manifest, content


@pytest.mark.parametrize("operation", ["create", "verify", "restore"])
def test_success_receipt_exact_hash_and_sanitized_output(tmp_path, operation):
    root, manifest, content = synthetic_manifest(tmp_path)
    output = tmp_path / "receipt.json"
    value = write_success_receipt(
        operation=operation, manifest=manifest, backup_root=root, destination=output
    )
    assert value.manifest_sha256 == hashlib.sha256(content).hexdigest()
    assert value.operation == operation
    assert BackupReceipt.model_validate_json(output.read_bytes()) == value
    assert (
        "private_inventory" not in output.read_text()
        and str(root) not in output.read_text()
    )
    with pytest.raises(FileExistsError):
        write_success_receipt(
            operation=operation, manifest=manifest, backup_root=root, destination=output
        )


def test_changed_manifest_marker_and_symlinks_fail_closed(tmp_path):
    root, manifest, _ = synthetic_manifest(tmp_path)
    (root / "COMPLETE").write_text("0" * 64 + "\n")
    with pytest.raises(BackupError):
        write_success_receipt(
            operation="create",
            manifest=manifest,
            backup_root=root,
            destination=tmp_path / "receipt.json",
        )
    assert not (tmp_path / "receipt.json").exists()


def test_cli_failed_command_cannot_issue_receipt(tmp_path, monkeypatch, capsys):
    from script import backup_v1

    output = tmp_path / "receipt.json"

    def failed(*args, **kwargs):
        raise BackupError("private credential must not print")

    monkeypatch.setattr(backup_v1, "verify_backup", failed)
    assert (
        backup_v1.main(
            [
                "verify",
                "--backup",
                str(tmp_path / "backup"),
                "--application-ref",
                APP,
                "--receipt-output",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()
    assert "private credential" not in capsys.readouterr().err


@pytest.mark.parametrize("operation", ["create", "verify", "restore"])
def test_cli_success_retains_exact_operation(tmp_path, monkeypatch, capsys, operation):
    from script import backup_v1

    root, manifest, _ = synthetic_manifest(tmp_path)
    output = tmp_path / f"{operation}.json"
    monkeypatch.setattr(backup_v1, "verify_backup", lambda *args, **kwargs: manifest)
    monkeypatch.setattr(backup_v1, "create_backup", lambda *args, **kwargs: manifest)
    monkeypatch.setattr(backup_v1, "restore_backup", lambda *args, **kwargs: manifest)
    monkeypatch.setattr(backup_v1, "PostgresDatabase", lambda *args, **kwargs: object())
    monkeypatch.setenv("SYNTHETIC_TEST_DATABASE", "not-used-or-printed")
    arguments = [
        operation,
        "--backup",
        str(root),
        "--application-ref",
        APP,
        "--receipt-output",
        str(output),
        "--storage",
        str(tmp_path / "storage"),
        "--durable-parent",
        str(tmp_path),
        "--database-url-env",
        "SYNTHETIC_TEST_DATABASE",
        "--acknowledge-quiesced",
        "--trust-backup-sql",
    ]
    assert backup_v1.main(arguments) == 0
    assert BackupReceipt.model_validate_json(output.read_bytes()).operation == operation
    assert "not-used-or-printed" not in capsys.readouterr().out


def test_receipt_cannot_be_written_into_backup_or_storage(tmp_path, monkeypatch):
    root, manifest, _ = synthetic_manifest(tmp_path)
    with pytest.raises(BackupError):
        write_success_receipt(
            operation="verify",
            manifest=manifest,
            backup_root=root,
            destination=root / "receipt.json",
        )
    from script import backup_v1

    def never(*args, **kwargs):
        pytest.fail("Receipt-in-storage refusal must occur before the backup command")

    monkeypatch.setattr(backup_v1, "verify_backup", never)
    assert (
        backup_v1.main(
            [
                "verify",
                "--backup",
                str(root),
                "--application-ref",
                APP,
                "--storage",
                str(tmp_path / "storage"),
                "--receipt-output",
                str(tmp_path / "storage" / "receipt.json"),
            ]
        )
        == 1
    )


def test_receipt_honors_the_successful_command_manifest_limit(tmp_path, monkeypatch):
    from backend.app import operations_backup

    root, manifest, _ = synthetic_manifest(tmp_path)
    configured = operations_backup.BackupLimits(max_manifest_bytes=64_000_000)
    original = operations_backup._read_json
    seen = []

    def read(root, key, limits):
        seen.append(limits)
        return original(root, key, limits)

    monkeypatch.setattr(operations_backup, "_read_json", read)
    write_success_receipt(
        operation="verify",
        manifest=manifest,
        backup_root=root,
        destination=tmp_path / "receipt.json",
        limits=configured,
    )
    assert seen == [configured]
