"""Loss-aware normalization; schema/quality failures are explicit, never filled."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from .calendars import coverage_for
from .contracts import (
    Bar,
    BarsRequest,
    Provenance,
    ProviderResult,
    RawPayload,
    UploadMapping,
    WarningRecord,
    utc_now,
    validate_portable_upload,
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def package_versions(names: list[str]) -> dict[str, str]:
    result = {}
    for name in names:
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = "not-installed"
    return result


# Authoritative public upstream pointers, not SDK licences or legal acceptance.
# These identify a terms source only; rights for a particular use remain unverified.
UPSTREAM_TERMS = {
    "eastmoney": "https://about.eastmoney.com/home/protocol",
    "yahoo": "https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html",
    "nasdaq": "https://www.nasdaq.com/legal",
}


def provenance(
    connector: str,
    provider: str,
    endpoint: str,
    requested_params: dict | None = None,
    effective_params: dict | None = None,
    evidence: str = "none",
    package_names: list[str] | None = None,
    **kwargs,
) -> Provenance:
    # Only caller-selected public request fields belong in artifacts.
    stamp = utc_now()
    identity = {
        "connector": connector,
        "provider": provider,
        "endpoint": endpoint,
        "effective": effective_params or {},
        "fetched_at": stamp.isoformat(),
    }
    return Provenance(
        provenance_id=hashlib.sha256(canonical_json(identity)).hexdigest(),
        connector=connector,
        provider=provider,
        endpoint=endpoint,
        requested_params=requested_params or {},
        effective_params=effective_params or {},
        evidence=evidence,
        package_versions=package_versions(package_names or []),
        fetched_at=stamp,
        terms_url=UPSTREAM_TERMS.get(provider),
        **kwargs,
    )


def frame_encoding(frame) -> dict:
    import pyarrow as pa

    table = pa.Table.from_pandas(frame, preserve_index=True)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return {
        "table": json.loads(
            frame.to_json(
                orient="split", date_format="iso", date_unit="ns", double_precision=15
            )
        ),
        "dtypes": {str(k): str(v) for k, v in frame.dtypes.items()},
        "index_name": frame.index.name,
        "index_frequency": getattr(frame.index, "freqstr", None),
        "attributes": frame.attrs,
        "arrow_ipc_base64": base64.b64encode(sink.getvalue().to_pybytes()).decode(
            "ascii"
        ),
    }


def frame_payload(
    frame, provenance_id: str, listing_id: str | None = None
) -> RawPayload:
    # Keep original columns, ordering, index and nulls before any normalization.
    return RawPayload(
        listing_id=listing_id,
        provenance_id=provenance_id,
        format="dataframe_split",
        content=frame_encoding(frame),
    )


def finish_result(request: BarsRequest, result: ProviderResult) -> ProviderResult:
    listings = {item.listing_id: item for item in request.listings}
    warnings = list(result.warnings)
    errors = {c.listing_id: c.error_code for c in result.coverage if c.error_code}
    for warning in warnings:
        if warning.severity == "error" and warning.listing_id:
            errors[warning.listing_id] = warning.code
    bars, seen = [], set()
    for bar in result.data:
        if bar.listing_id not in listings:
            raise ValueError("upstream returned an unrequested listing")
        if not request.start <= bar.session_date < request.end:
            warnings.append(
                WarningRecord(
                    code="outside_requested_range",
                    listing_id=bar.listing_id,
                    message="Upstream boundary row retained only in raw payload",
                )
            )
            continue
        key = (bar.listing_id, bar.session_date)
        reason = None
        numeric = [
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.amount,
            bar.adj_close,
        ]
        if any(value is not None and not math.isfinite(value) for value in numeric):
            reason = "nonfinite_bar"
        elif key in seen:
            reason = "duplicate_bar"
        elif (
            listings[bar.listing_id].listed_on
            and bar.session_date < listings[bar.listing_id].listed_on
        ) or (
            listings[bar.listing_id].delisted_on
            and bar.session_date >= listings[bar.listing_id].delisted_on
        ):
            reason = "outside_listing_lifetime"
        elif bar.volume < 0 or (bar.amount is not None and bar.amount < 0):
            reason = "negative_volume_or_amount"
        elif min(bar.open, bar.high, bar.low, bar.close) <= 0:
            reason = "nonpositive_price"
        elif bar.low > min(bar.open, bar.close, bar.high) or bar.high < max(
            bar.open, bar.close, bar.low
        ):
            reason = "invalid_ohlc"
        if reason:
            errors[bar.listing_id] = reason
            warnings.append(
                WarningRecord(
                    code=reason,
                    message="Invalid row retained only in raw payload",
                    listing_id=bar.listing_id,
                    severity="error",
                )
            )
            continue
        seen.add(key)
        bars.append(bar)
    bars.sort(key=lambda bar: (bar.listing_id, bar.session_date))
    coverage = [
        coverage_for(
            item,
            request,
            [bar for bar in bars if bar.listing_id == item.listing_id],
            error_code=errors.get(item.listing_id),
        )
        for item in request.listings
    ]
    provisional = {
        (item.listing_id, day) for item in coverage for day in item.provisional_sessions
    }
    bars = [
        bar.model_copy(update={"flags": sorted(set(bar.flags + ["provisional"]))})
        if (bar.listing_id, bar.session_date) in provisional
        else bar
        for bar in bars
    ]
    if any(a.listing_id not in listings for a in result.actions):
        raise ValueError("upstream returned actions for an unrequested listing")
    actions = [
        a for a in result.actions if request.start <= a.session_date < request.end
    ]
    semantics = result.price_semantics
    # Neither request coverage cutoff nor SDK acquisition time establishes an
    # adjustment-state timestamp. Preserve only explicitly supplied semantics.
    if actions:
        snapshot_id = hashlib.sha256(
            canonical_json([a.model_dump(mode="json") for a in actions])
        ).hexdigest()
        semantics = semantics.model_copy(update={"actions_snapshot_id": snapshot_id})
    return result.model_copy(
        update={
            "data": bars,
            "coverage": coverage,
            "warnings": warnings,
            "actions": actions,
            "price_semantics": semantics,
        }
    )


def normalize_upload(
    path: Path,
    mapping: UploadMapping,
    request: BarsRequest,
    source_reference: dict[str, Any] | None = None,
) -> ProviderResult:
    """Normalize one immutable original once, with bounded parsing and exact raw bytes.

    Worker references must name a bundle artifact whose original bytes it copies
    unchanged. Standalone callers retain the same original inline, never an
    expanded/repeated DataFrame encoding. One listing and daily intervals only.
    """
    import csv
    import io
    import re

    import pandas as pd

    max_rows, max_memory = 100_000, 100_000_000
    if len(request.listings) != 1:
        raise ValueError("One listing per uploaded table is required")
    listing = request.listings[0]
    validate_portable_upload(mapping, start=request.start, end=request.end)
    if mapping.portable_declaration is not None and source_reference is None:
        raise ValueError(
            "Portable declaration requires a pinned original upload artifact"
        )
    from .venues import canonical_calendar

    requested_calendar = canonical_calendar(
        listing.mic,
        listing.calendar,
        currency=listing.currency,
        timezone=listing.timezone,
    )
    mapped_calendar = canonical_calendar(
        mapping.mic,
        mapping.calendar,
        currency=mapping.currency,
        timezone=mapping.timezone,
    )
    if (
        listing.mic != mapping.mic
        or listing.symbol != mapping.symbol
        or requested_calendar != mapped_calendar
    ):
        raise ValueError("Upload mapping does not match the requested listing identity")
    with path.open("rb") as file:
        original = file.read(10_000_001)
    if len(original) > 10_000_000:
        raise ValueError("Upload exceeds the 10 MB limit")
    extension = path.suffix.lower()
    if extension not in {".csv", ".parquet"}:
        raise ValueError("Only CSV and Parquet uploads are supported")
    digest = hashlib.sha256(original).hexdigest()
    raw_content = {"sha256": digest, "size_bytes": len(original)}
    if source_reference is not None:
        artifact = source_reference.get("artifact", "")
        if (
            not isinstance(artifact, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+\.(csv|parquet)", artifact)
            or Path(artifact).suffix != extension
            or source_reference.get("sha256") != digest
            or source_reference.get("size_bytes") != len(original)
        ):
            raise ValueError(
                "Upload source reference must match safe artifact and exact original bytes"
            )
        raw_content["artifact"] = artifact
    else:
        raw_content["bytes_base64"] = base64.b64encode(original).decode("ascii")
    required = {"session_date", "open", "high", "low", "close", "volume"}
    if not required <= mapping.columns.keys():
        raise ValueError("Upload column mapping does not match the table")
    selected = list(dict.fromkeys(mapping.columns.values()))
    action_columns = []

    def include_action_columns(names):
        # These conventional action columns must not be discarded by column mapping.
        # Their absence still proves nothing; the opt-in remains a user assumption.
        if mapping.portable_declaration is not None:
            action_columns.extend(
                name
                for name in names
                if re.sub(r"[ _-]", "", name).lower()
                in {
                    "dividend",
                    "dividends",
                    "split",
                    "splits",
                    "stocksplit",
                    "stocksplits",
                    "capitalgain",
                    "capitalgains",
                }
            )
            selected.extend(name for name in action_columns if name not in selected)

    if len(selected) > 8:
        raise ValueError("Upload supports at most eight mapped columns")
    if extension == ".csv":
        records = csv.reader(io.StringIO(original.decode("utf-8-sig")), strict=True)
        header = next(records, [])
        if not 1 <= len(header) <= 50 or len(set(header)) != len(header):
            raise ValueError("Upload requires 1 to 50 uniquely named columns")
        count = 0
        for record in records:
            # Empty physical lines are harmless separators; nonempty records must
            # match exactly so pandas cannot silently infer a shifted index.
            if not record:
                continue
            count += 1
            if len(record) != len(header):
                raise ValueError("CSV record width does not match its header")
            if count > max_rows:
                raise ValueError("Upload exceeds the 100000 daily-row limit")
        if not set(selected) <= set(header):
            raise ValueError("Upload column mapping does not match the table")
        include_action_columns(header)
        frame = pd.read_csv(io.BytesIO(original), usecols=selected, nrows=max_rows + 1)
    else:
        import pyarrow as pa
        import pyarrow.parquet as pq

        parquet = pq.ParquetFile(io.BytesIO(original))
        metadata = parquet.metadata
        if (
            metadata.num_rows > max_rows
            or len(parquet.schema_arrow) > 50
            or sum(
                metadata.row_group(i).total_byte_size
                for i in range(metadata.num_row_groups)
            )
            > max_memory
        ):
            raise ValueError(
                "Expanded Parquet exceeds the row, column or 100 MB size limit"
            )
        if any(pa.types.is_nested(field.type) for field in parquet.schema_arrow):
            raise ValueError("Nested Parquet fields are unsupported")
        if not set(selected) <= set(parquet.schema_arrow.names):
            raise ValueError("Upload column mapping does not match the table")
        include_action_columns(parquet.schema_arrow.names)
        frame = parquet.read(columns=selected).to_pandas()
    if len(frame) > max_rows:
        raise ValueError("Upload exceeds the 100000 daily-row limit")
    if frame.memory_usage(index=True, deep=True).sum() > max_memory:
        raise ValueError("Upload table exceeds the 100 MB in-memory limit")
    source = provenance(
        "upload",
        "user",
        "uploaded_table",
        evidence="uploaded",
        requested_params={
            "listing_id": listing.listing_id,
            "start": str(request.start),
            "end": str(request.end),
        }
        if mapping.portable_declaration is not None
        else None,
        effective_params=mapping.model_dump(mode="json")
        | (
            {"upload_source": raw_content}
            if mapping.portable_declaration is not None
            else {}
        ),
        package_names=["pandas", "pyarrow"],
        note="Exact original bytes retained; only explicitly mapped columns parsed",
    )
    raw = RawPayload(
        listing_id=listing.listing_id,
        provenance_id=source.provenance_id,
        format=extension[1:],
        content=raw_content,
        description="Exact original upload bytes, inline or referenced immutable bundle artifact",
    )
    bars, warnings, invalid_rows = [], [], 0
    for values in frame.itertuples(index=False, name=None):
        row = dict(zip(frame.columns, values, strict=True))
        try:
            day_value = row[mapping.columns["session_date"]]
            if isinstance(day_value, (datetime, pd.Timestamp)):
                day = day_value.date()
            else:
                day = datetime.strptime(str(day_value), mapping.date_format).date()
            if not request.start <= day < request.end:
                continue
            for column in action_columns:
                value = float(row[column])
                if not math.isfinite(value) or value != 0:
                    raise ValueError(
                        "Declared no-action upload contains an action or unknown action value"
                    )
            fields = {
                name: row[column]
                for name, column in mapping.columns.items()
                if name
                in {"open", "high", "low", "close", "volume", "amount", "adj_close"}
            }
            bars.append(
                Bar(
                    listing_id=listing.listing_id,
                    session_date=day,
                    currency=mapping.currency,
                    volume_unit=mapping.volume_unit,
                    **fields,
                )
            )
        except (ValueError, TypeError):
            invalid_rows += 1
    if invalid_rows:
        warnings.append(
            WarningRecord(
                code="invalid_uploaded_row",
                severity="error",
                listing_id=listing.listing_id,
                message=f"{invalid_rows} uploaded rows could not be normalized; exact source retained",
            )
        )
    if mapping.portable_declaration is not None:
        warnings.append(
            WarningRecord(
                code="portable_upload_assumption_unverified",
                listing_id=listing.listing_id,
                message="Raw prices and absence of corporate actions are user-declared research assumptions for this exact listing/range. Source references are retained, not independently verified; no provider, runtime or cross-framework certification.",
            )
        )
    if (
        mapping.currency == "unknown"
        or mapping.volume_unit == "unknown"
        or (
            mapping.price_semantics.split_basis == "unknown"
            or mapping.price_semantics.dividend_basis == "unknown"
        )
    ):
        warnings.append(
            WarningRecord(
                code="unknown_uploaded_semantics",
                listing_id=listing.listing_id,
                message="Unknown uploaded units or price basis are preserved, not inferred",
            )
        )
    return finish_result(
        request,
        ProviderResult(
            data=bars,
            raw=[raw],
            provenance=[source],
            warnings=warnings,
            price_semantics=mapping.price_semantics,
        ),
    )
