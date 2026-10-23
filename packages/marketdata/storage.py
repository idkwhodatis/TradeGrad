"""Write a complete staging bundle. The worker owns atomic immutable publication."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from pathlib import Path

from .contracts import SCHEMA_VERSION, TRANSFORM_VERSION, BarsRequest, ProviderResult
from .normalize import canonical_json, package_versions


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_bundle(directory: Path, request: BarsRequest, result: ProviderResult) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("Bundle staging directory cannot be a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise FileExistsError("Refusing to overwrite a staged or published bundle")
    bars = [bar.model_dump(mode="json") for bar in result.data]
    artifact_files = {}

    def write(name, value):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(canonical_json(value))
        artifact_files[name] = sha256_file(path)

    schema = pa.schema(
        [
            ("listing_id", pa.string()),
            ("session_date", pa.date32()),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("volume", pa.float64()),
            ("adj_close", pa.float64()),
            ("amount", pa.float64()),
            ("currency", pa.string()),
            ("volume_unit", pa.string()),
            ("flags", pa.list_(pa.string())),
        ],
        metadata={
            b"contract": SCHEMA_VERSION.encode(),
            b"transform": TRANSFORM_VERSION.encode(),
        },
    )
    records = [bar.model_dump() for bar in result.data]
    table = pa.Table.from_pylist(records, schema=schema)
    pq.write_table(
        table, directory / "normalized.parquet", compression="zstd", version="2.6"
    )
    artifact_files["normalized.parquet"] = sha256_file(directory / "normalized.parquet")
    write("raw.json", [raw.model_dump(mode="json") for raw in result.raw])
    write(
        "provenance.json",
        [entry.model_dump(mode="json") for entry in result.provenance],
    )
    write("coverage.json", [entry.model_dump(mode="json") for entry in result.coverage])
    write("quality.json", [entry.model_dump(mode="json") for entry in result.warnings])
    write("actions.json", [entry.model_dump(mode="json") for entry in result.actions])
    # Created only in the separately locked data runtime, never from price rows.
    from .calendars import session_schedule
    from .portable_fixture import action_coverage
    from .upload_admission import (
        DECLARATION_FILE,
        declared_action_coverage,
        declared_upload_evidence,
    )

    write("sessions.json", session_schedule(request))
    declaration = declared_upload_evidence(request, result)
    if declaration is not None:
        write(DECLARATION_FILE, declaration)
    write(
        "corporate-action-coverage.json",
        (
            declared_action_coverage(declaration)
            if declaration is not None
            else action_coverage(request, result)
        ),
    )
    write("preview.json", bars)
    evaluator_files, symbol_mapping = {}, {}
    for listing in request.listings:
        if len(request.listings) == 1:
            symbol = "INDEX"
        else:
            symbol = listing.symbol
            if listing.mic in {"XSHG", "XSHE"}:
                symbol = ("sh." if listing.mic == "XSHG" else "sz.") + symbol
        # Never interpret a vendor/user symbol as an arbitrary filesystem path.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", symbol):
            raise ValueError("Unsafe evaluator symbol")
        if symbol in symbol_mapping.values():
            raise ValueError("Evaluator symbol collision across listings")
        symbol_mapping[listing.listing_id] = symbol
        output = io.StringIO(newline="")
        writer = csv.DictWriter(
            output,
            fieldnames=["date", "open", "high", "low", "close", "volume", "amount"],
        )
        writer.writeheader()
        for bar in result.data:
            if bar.listing_id == listing.listing_id:
                writer.writerow(
                    {
                        "date": bar.session_date.isoformat(),
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                        "amount": "" if bar.amount is None else bar.amount,
                    }
                )
        name = f"data/{symbol}.csv"
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as file:
            file.write(output.getvalue().encode("utf-8"))
        evaluator_files[name] = sha256_file(path)
    # A requested basket is user_fixed, never a historical/PIT index constituent claim.
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=["updateDate", "code", "code_name"])
    writer.writeheader()
    for listing in request.listings:
        writer.writerow(
            {
                "updateDate": request.start.isoformat(),
                "code": symbol_mapping[listing.listing_id],
                "code_name": listing.name or listing.symbol,
            }
        )
    name = f"universe/fixed_list_{request.start:%Y-%m}.csv"
    (directory / "universe").mkdir()
    (directory / name).write_text(output.getvalue(), encoding="utf-8")
    evaluator_files[name] = sha256_file(directory / name)
    # Stable content identity includes semantics, mappings and exact artifact hashes.
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "transformation_version": TRANSFORM_VERSION,
        "request": request.model_dump(mode="json"),
        "listings": [x.model_dump(mode="json") for x in request.listings],
        "price_semantics": result.price_semantics.model_dump(mode="json"),
        "derivation": request.repair_source.model_dump(mode="json")
        if request.repair_source
        else None,
        "coverage": [x.model_dump(mode="json") for x in result.coverage],
        "warnings": [x.model_dump(mode="json") for x in result.warnings],
        "provenance": [x.model_dump(mode="json") for x in result.provenance],
        "files": evaluator_files,
        "artifact_files": artifact_files,
        "evaluator_symbol_mapping": symbol_mapping,
        "universe": {
            "state": "user_fixed",
            "point_in_time": False,
            "note": "Explicit requested basket; not historical index membership",
        },
        "package_versions": package_versions(
            ["pandas", "pyarrow", "exchange-calendars"]
        ),
        "row_count": len(bars),
        "bars_sha256": hashlib.sha256(canonical_json(bars)).hexdigest(),
        "ready": bool(result.data)
        and len(result.coverage) == len(request.listings)
        and all(c.status == "complete" for c in result.coverage)
        and not any(w.severity == "error" for w in result.warnings),
    }
    if declaration is not None:
        manifest["data_evidence"] = "user_declaration_unverified"
        manifest["is_synthetic"] = False
    manifest["content_hash"] = hashlib.sha256(canonical_json(manifest)).hexdigest()
    with (directory / "manifest.json").open("xb") as file:
        file.write(canonical_json(manifest))
    return manifest


def verify_bundle(directory: Path, manifest: dict) -> None:
    expected = dict(manifest)
    digest = expected.pop("content_hash")
    if hashlib.sha256(canonical_json(expected)).hexdigest() != digest:
        raise ValueError("Manifest content hash mismatch")
    for name, digest in (manifest["files"] | manifest["artifact_files"]).items():
        path = directory / name
        if path.is_symlink() or directory.resolve() not in path.resolve().parents:
            raise ValueError("Unsafe artifact path")
        if sha256_file(path) != digest:
            raise ValueError("Artifact hash mismatch")
