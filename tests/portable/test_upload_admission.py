"""Inert upload-to-staging contract tests, not market/runtime certification.

All table bytes below are artificial test inputs. No policy/SDK/model is executed.
"""

import copy
import csv
import hashlib
import io
import json
from datetime import UTC, date, datetime

import pytest
from test_staging import fixture, repin

from backend.app.data_schemas import UploadMapping as PublicUploadMapping
from packages.marketdata.contracts import (
    BarsRequest,
    CorporateAction,
    Listing,
    UploadMapping,
)
from packages.marketdata.normalize import normalize_upload
from packages.marketdata.storage import write_bundle
from packages.marketdata.upload_admission import declared_upload_evidence
from packages.tradegrad_adapter.portable_windows import stage_portable_dataset_window


def mapping_values():
    return {
        "symbol": "AAPL",
        "mic": "XNAS",
        "currency": "USD",
        "timezone": "America/New_York",
        "calendar": "XNYS",
        "volume_unit": "shares",
        "columns": {
            k: k for k in ("session_date", "open", "high", "low", "close", "volume")
        },
        "price_semantics": {
            "native_mode": "raw",
            "split_basis": "unadjusted",
            "dividend_basis": "unadjusted",
        },
        "portable_declaration": {
            "kind": "user_declared_raw_no_actions",
            "acknowledged_unverified": True,
            "symbol": "AAPL",
            "mic": "XNAS",
            "start": "2024-01-02",
            "end": "2024-01-11",
            "source_reference": "test source reference, not independent verification",
            "evidence_note": "Test-only user statement for the exact listing and interval; not market evidence.",
        },
    }


def uploaded(
    tmp_path,
    *,
    action_column=None,
    action_value=None,
    declaration=True,
    file_format="csv",
):
    config, fixture_manifest, fixture_blobs, *_ = fixture()
    listing = Listing.model_validate(
        fixture_manifest["listings"][0]
        | {
            "identity_status": "user_supplied",
            "calendar_note": "XNAS uses declared XNYS schedule; proxy equivalence is not certified.",
        }
    )
    req = BarsRequest(
        listings=[listing],
        start=date(2024, 1, 2),
        end=date(2024, 1, 11),
        as_of=datetime(2025, 1, 2, tzinfo=UTC),
    )
    values = mapping_values()
    if not declaration:
        values.pop("portable_declaration")
    mapping = UploadMapping.model_validate(values)
    columns = list(mapping.columns)
    if action_column:
        columns.append(action_column)
    content = io.StringIO(newline="")
    writer = csv.DictWriter(content, fieldnames=columns)
    writer.writeheader()
    rows = []
    for row in json.loads(fixture_blobs["preview.json"]):
        selected = {k: row[k] for k in mapping.columns}
        if action_column:
            selected[action_column] = action_value
        writer.writerow(selected)
        rows.append(selected)
    original = content.getvalue().encode()
    if file_format == "parquet":
        import pyarrow as pa
        import pyarrow.parquet as pq

        sink = pa.BufferOutputStream()
        pq.write_table(pa.Table.from_pylist(rows), sink)
        original = sink.getvalue().to_pybytes()
    source_name = f"source.{file_format}"
    path = tmp_path / source_name
    path.write_bytes(original)
    reference = {
        "artifact": source_name,
        "sha256": hashlib.sha256(original).hexdigest(),
        "size_bytes": len(original),
    }
    result = normalize_upload(path, mapping, req, source_reference=reference)
    directory = tmp_path / "bundle"
    manifest = write_bundle(directory, req, result)
    # Same final original-byte binding as the worker's publication step.
    (directory / source_name).write_bytes(original)
    manifest["artifact_files"][source_name] = reference["sha256"]
    manifest["upload_source"] = reference
    blobs = {
        name: (directory / name).read_bytes() for name in manifest["artifact_files"]
    }
    return repin(config, manifest, blobs), manifest, blobs, req, result


def rebind(config, manifest, blobs, evidence=None, coverage=None):
    if evidence is not None:
        blobs["portable-upload-declaration.json"] = json.dumps(
            evidence, sort_keys=True, separators=(",", ":")
        ).encode()
        coverage = coverage or json.loads(blobs["corporate-action-coverage.json"])
        coverage["declaration"]["sha256"] = hashlib.sha256(
            blobs["portable-upload-declaration.json"]
        ).hexdigest()
    if coverage is not None:
        blobs["corporate-action-coverage.json"] = json.dumps(
            coverage, sort_keys=True, separators=(",", ":")
        ).encode()
    return repin(config, manifest, blobs)


@pytest.mark.parametrize("file_format", ["csv", "parquet"])
def test_explicit_upload_stages_as_assumption_not_synthetic_or_verified(
    tmp_path, file_format
):
    config, manifest, blobs, _, _ = uploaded(tmp_path, file_format=file_format)
    assert manifest["ready"]
    staged = stage_portable_dataset_window(config, "train", manifest, blobs.__getitem__)
    assert not staged.report.is_synthetic and not staged.report.point_in_time_proven
    assert staged.report.data_evidence == "user_declaration_unverified"
    assert any(
        "not independently verified" in warning for warning in staged.report.warnings
    )
    coverage = json.loads(blobs["corporate-action-coverage.json"])
    assert coverage["listings"][0]["status"] == "declared_none"
    assert coverage["is_synthetic"] is False
    assert coverage["listings"][0]["evidence"] != "synthetic_by_construction"
    assert manifest["provenance"][0]["evidence"] == "uploaded"
    assert staged.market["corporate_actions"] == []
    assert all(bar["session_date"] < "2024-01-09" for bar in staged.market["bars"])


def test_ordinary_raw_upload_with_empty_actions_still_rejects(tmp_path):
    config, manifest, blobs, _, _ = uploaded(tmp_path, declaration=False)
    assert manifest["ready"]
    assert (
        json.loads(blobs["corporate-action-coverage.json"])["listings"][0]["status"]
        == "unknown"
    )
    with pytest.raises(ValueError, match="synthetic evidence"):
        stage_portable_dataset_window(config, "train", manifest, blobs.__getitem__)


@pytest.mark.parametrize(
    "path,value",
    [
        (("portable_declaration", "acknowledged_unverified"), False),
        (("portable_declaration", "acknowledged_unverified"), 1),
        (("portable_declaration", "acknowledged_unverified"), "true"),
        (("portable_declaration", "kind"), "verified_no_actions"),
        (("portable_declaration", "symbol"), "MSFT"),
        (("portable_declaration", "mic"), "XNYS"),
        (("portable_declaration", "source_reference"), " "),
        (("portable_declaration", "source_reference"), "x" * 513),
        (("portable_declaration", "evidence_note"), "x" * 2001),
        (("portable_declaration", "evidence_note"), "bad\ncontrol"),
        (("portable_declaration", "end"), "2024-01-02"),
        (("price_semantics", "native_mode"), "provider_native"),
        (("price_semantics", "native_mode"), "user_declared_synthetic"),
        (("price_semantics", "split_basis"), "adjusted"),
        (("price_semantics", "dividend_basis"), "unknown"),
        (("price_semantics", "algorithm_version"), "upstream-adjustment.v1"),
        (("price_semantics", "actions_snapshot_id"), "invented"),
        (("volume_unit",), "unknown"),
        (("currency",), "unknown"),
    ],
)
def test_declarations_fail_closed_at_mapping_boundary(path, value):
    values = mapping_values()
    node = values
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValueError):
        UploadMapping.model_validate(values)


@pytest.mark.parametrize(
    "change",
    [
        {"start_date": "2024-01-03"},
        {"end_date": "2024-01-12"},
        {"unknown_flags": ["unresolved"]},
    ],
)
def test_public_mapping_requires_exact_effective_bounds_and_no_unknown_flags(change):
    values = mapping_values() | {
        "market": "US",
        "start_date": "2024-01-02",
        "end_date": "2024-01-11",
    }
    assert PublicUploadMapping.model_validate(values).portable_declaration is not None
    with pytest.raises(ValueError):
        PublicUploadMapping.model_validate(values | change)


@pytest.mark.parametrize(
    "column,value",
    [
        ("Dividends", 1),
        ("Stock Splits", 2),
        ("Capital Gains", 0.2),
        ("splits", ""),
        ("dividend", "unknown"),
    ],
)
@pytest.mark.parametrize("file_format", ["csv", "parquet"])
def test_known_or_unknown_action_columns_cannot_be_discarded(
    tmp_path, column, value, file_format
):
    _, manifest, _, _, result = uploaded(
        tmp_path, action_column=column, action_value=value, file_format=file_format
    )
    assert not manifest["ready"]
    assert any(
        w.code == "invalid_uploaded_row" and w.severity == "error"
        for w in result.warnings
    )


def test_zero_action_columns_do_not_upgrade_declaration_to_proof(tmp_path):
    config, manifest, blobs, _, _ = uploaded(
        tmp_path, action_column="Dividends", action_value=0
    )
    staged = stage_portable_dataset_window(config, "train", manifest, blobs.__getitem__)
    assert staged.report.data_evidence == "user_declaration_unverified"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_declaration",
        "missing_source",
        "source_bytes",
        "missing_ack",
        "wrong_listing",
        "wrong_start",
        "wrong_end",
        "missing_reference",
        "verified_label",
        "known_none",
        "foreign_provenance",
        "adjusted",
        "unknown_price",
        "raw_ref",
        "source_ref",
        "source_size",
        "action",
        "unknown_action",
        "unidentified_action",
        "undated_action",
        "null_action_date",
        "synthetic_label",
        "past_action",
        "bar_gap",
    ],
)
def test_staging_revalidates_immutable_user_claims_without_inference(
    tmp_path, mutation
):
    config, manifest, blobs, _, _ = uploaded(tmp_path)
    evidence = json.loads(blobs["portable-upload-declaration.json"])
    coverage = json.loads(blobs["corporate-action-coverage.json"])
    if mutation == "missing_declaration":
        del blobs["portable-upload-declaration.json"]
    elif mutation == "missing_source":
        del blobs["source.csv"]
    elif mutation == "source_bytes":
        blobs["source.csv"] += b"tamper"
    elif mutation == "missing_ack":
        del evidence["declaration"]["acknowledged_unverified"]
    elif mutation == "wrong_listing":
        evidence["listing_id"] = "foreign"
    elif mutation == "wrong_start":
        evidence["start"] = "2024-01-03"
    elif mutation == "wrong_end":
        evidence["end"] = "2024-01-10"
    elif mutation == "missing_reference":
        evidence["declaration"]["source_reference"] = ""
    elif mutation == "verified_label":
        evidence["data_evidence"] = "verified"
    elif mutation == "synthetic_label":
        manifest["is_synthetic"] = True
    elif mutation == "known_none":
        coverage["listings"][0]["status"] = "known_none"
    elif mutation == "foreign_provenance":
        manifest["provenance"][0].update(
            connector="yfinance", provider="yahoo", evidence="live"
        )
    elif mutation in {"adjusted", "unknown_price"}:
        manifest["price_semantics"]["split_basis"] = (
            "adjusted" if mutation == "adjusted" else "unknown"
        )
    elif mutation == "raw_ref":
        raw = json.loads(blobs["raw.json"])
        raw[0]["content"]["sha256"] = "0" * 64
        blobs["raw.json"] = json.dumps(raw).encode()
    elif mutation == "source_ref":
        evidence["source"] = evidence["source"] | {"artifact": "../source.csv"}
    elif mutation == "source_size":
        evidence["source"] = evidence["source"] | {"size_bytes": 10_000_001}
    elif mutation == "bar_gap":
        bars = json.loads(blobs["preview.json"])
        bars.pop(0)
        blobs["preview.json"] = json.dumps(bars).encode()
    else:
        action = {
            "listing_id": manifest["listings"][0]["listing_id"],
            "session_date": "2024-01-04",
            "action_type": "dividend",
        }
        if mutation == "unknown_action":
            action["action_type"] = "merger"
        if mutation == "unidentified_action":
            action["listing_id"] = "foreign"
        if mutation == "undated_action":
            del action["session_date"]
        if mutation == "null_action_date":
            action["session_date"] = None
        if mutation == "past_action":
            action["session_date"] = "2024-01-02"
        blobs["actions.json"] = json.dumps([action]).encode()
    if mutation not in {"missing_declaration", "missing_source", "source_bytes"}:
        config = rebind(config, manifest, blobs, evidence, coverage)
    else:
        config = repin(config, manifest, blobs)
    with pytest.raises(ValueError):
        stage_portable_dataset_window(config, "train", manifest, blobs.__getitem__)


def test_publication_rejects_action_present_or_misbound_declarations(tmp_path):
    _, _, _, req, result = uploaded(tmp_path)
    action = CorporateAction(
        listing_id=req.listings[0].listing_id,
        session_date=date(2024, 1, 4),
        action_type="dividend",
        value=1,
        provenance_id="action-source",
    )
    with pytest.raises(ValueError, match="actions"):
        declared_upload_evidence(req, result.model_copy(update={"actions": [action]}))
    with pytest.raises(ValueError, match="provenance"):
        declared_upload_evidence(
            req.model_copy(update={"start": date(2024, 1, 3)}), result
        )


def test_upload_semantics_and_declared_evidence_cannot_change_without_new_hash(
    tmp_path,
):
    config, manifest, blobs, _, _ = uploaded(tmp_path)
    changed = copy.deepcopy(manifest)
    changed["provenance"][0]["effective_params"]["portable_declaration"][
        "evidence_note"
    ] = "new claim"
    with pytest.raises(ValueError, match="manifest changed"):
        stage_portable_dataset_window(config, "train", changed, blobs.__getitem__)
