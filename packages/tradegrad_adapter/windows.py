"""Bounded CSV preflight and split staging without provider/array dependencies.

Dates are exchange session labels, not timezone-naive instants. Previous available
rows are counted per listing; a calendar gap is never filled into a tradable bar.
Coverage is only checked against the pinned schedule evidence when provided. A
proxy calendar and native arbitrary code do not establish point-in-time safety.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Literal

from .config import DateWindow, RunConfig, Split, canonical_bytes, evaluation_windows

COLD_START_WARNING = (
    "The transitional fixed-seed runner stages only rows in `[start, end)`. It does not "
    "stage earlier rows as warm-up history, even when an immutable snapshot contains "
    "them. AKQuant can consume pre-start history, but this adapter does not yet supply "
    "it. The unchanged timing seeds request 20 closes; AKQuant's initial NaN padding "
    "can make the original US timing seed select shorts before its history is full. "
    "Cross-sectional seed history requirements likewise need explicit preflight."
)
# Historical text above is retained verbatim for the source-compatible cold-start
# option only. Corrected staging below does supply bounded pre-start history.


class PreflightError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        listing_id: str | None = None,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.code, self.message, self.listing_id, self.details = (
            code,
            message,
            listing_id,
            details or {},
        )


@dataclass(frozen=True)
class CsvLimits:
    max_file_bytes: int = 64_000_000
    max_total_bytes: int = 500_000_000
    max_rows: int = 500_000
    max_files: int = 500
    max_columns: int = 64
    max_cell_chars: int = 16_384

    def __post_init__(self):
        if any(
            not isinstance(value, int) or value <= 0 for value in vars(self).values()
        ):
            raise ValueError("CSV limits must be positive integers")


DEFAULT_LIMITS = CsvLimits()


@dataclass(frozen=True)
class CsvWindowReport:
    listing_id: str
    window: DateWindow
    required_warmup_sessions: int
    staged_warmup_sessions: int
    evaluation_rows: int
    staged_start: date
    staged_last: date
    source_sha256: str
    staged_sha256: str
    expected_session_check: Literal["pinned_schedule", "unavailable"]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class StagedCsv:
    content: bytes
    report: CsvWindowReport


@dataclass(frozen=True)
class DatasetWindowReport:
    split: Split
    window: DateWindow
    listings: tuple[CsvWindowReport, ...]
    warnings: tuple[str, ...]
    universe_state: str
    point_in_time_proven: Literal[False] = False


@dataclass(frozen=True)
class StagedDataset:
    files: Mapping[str, bytes]
    report: DatasetWindowReport


def _day(value: object) -> date:
    if isinstance(value, datetime):
        raise PreflightError(
            "invalid_session", "Session must be an ISO date, not a timestamp"
        )
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise PreflightError(
            "invalid_session", "Session must have exact YYYY-MM-DD format"
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PreflightError(
            "invalid_session", "Session is not a valid calendar date"
        ) from exc


def _rows(data: bytes, limits: CsvLimits):
    if not isinstance(data, bytes) or len(data) > limits.max_file_bytes:
        raise PreflightError("csv_byte_budget", "CSV exceeds the per-file byte budget")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise PreflightError("invalid_csv_encoding", "CSV must be UTF-8") from exc
    if "\x00" in text:
        raise PreflightError("invalid_csv", "CSV contains a NUL byte")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
        if (
            not header
            or len(header) > limits.max_columns
            or any(not h or len(h) > 128 for h in header)
            or len(set(header)) != len(header)
        ):
            raise PreflightError(
                "invalid_csv_header",
                "CSV requires unique, nonempty, bounded column names",
            )
        yield header
        for number, row in enumerate(reader, 1):
            if number > limits.max_rows:
                raise PreflightError(
                    "csv_row_budget", "CSV exceeds the per-file row budget"
                )
            if len(row) != len(header):
                raise PreflightError(
                    "invalid_csv_width",
                    f"CSV row {number} has an inconsistent number of columns",
                )
            if any(len(cell) > limits.max_cell_chars for cell in row):
                raise PreflightError(
                    "csv_cell_budget", "CSV cell exceeds the character budget"
                )
            yield row
    except csv.Error as exc:
        raise PreflightError(
            "invalid_csv", "CSV quoting or field length is invalid"
        ) from exc


def _verify(data: bytes, expected: str | None):
    digest = hashlib.sha256(data).hexdigest()
    if expected is not None and digest != expected:
        raise PreflightError(
            "input_hash_mismatch",
            "Immutable input bytes changed; republish a new snapshot",
        )
    return digest


def stage_csv_window(
    data: bytes,
    *,
    listing_id: str,
    window: DateWindow,
    warmup_sessions: int,
    history_policy: Literal["require", "source_cold_start"] = "require",
    expected_sha256: str | None = None,
    expected_sessions: Sequence[date | str] | None = None,
    required_fields: tuple[str, ...] = (
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ),
    limits: CsvLimits = DEFAULT_LIMITS,
) -> StagedCsv:
    """Keep last N available pre-start rows plus [start,end), independently per listing.

    No earlier extra history or >=end row is emitted. Missing pinned evaluation
    sessions fail closed; gaps outside the evaluation window are not fabricated.
    A missing schedule means only observed-row coverage can be asserted.
    """
    if not isinstance(warmup_sessions, int) or not 0 <= warmup_sessions <= 252:
        raise PreflightError(
            "invalid_warmup", "Warm-up must be between 0 and 252 sessions"
        )
    if history_policy not in ("require", "source_cold_start") or (
        history_policy == "source_cold_start" and warmup_sessions != 0
    ):
        raise PreflightError(
            "invalid_warmup", "Source-compatible cold start requires zero warm-up"
        )
    if len(data) > limits.max_file_bytes:
        raise PreflightError("csv_byte_budget", "CSV exceeds the per-file byte budget")
    source_digest = _verify(data, expected_sha256)
    rows = _rows(data, limits)
    header = next(rows)
    if not set(required_fields).issubset(header) or "date" not in header:
        raise PreflightError(
            "missing_fields",
            "CSV is missing required strategy fields",
            listing_id=listing_id,
            details={"required": list(required_fields)},
        )
    date_column = header.index("date")
    numeric_columns = [
        header.index(name)
        for name in ("open", "high", "low", "close", "volume")
        if name in header
    ]
    previous = deque(maxlen=warmup_sessions)
    selected = []
    last = None
    for row in rows:
        day = _day(row[date_column])
        if last is not None and day <= last:
            raise PreflightError(
                "unordered_sessions",
                "CSV sessions must be strictly increasing with no duplicate dates",
                listing_id=listing_id,
            )
        last = day
        if day >= window.end:
            # Validate syntax/order of all source bytes, but do not retain future rows.
            continue
        for index in numeric_columns:
            try:
                value = float(row[index])
            except ValueError as exc:
                raise PreflightError(
                    "invalid_bar",
                    "OHLCV values must be finite numbers",
                    listing_id=listing_id,
                ) from exc
            if (
                not math.isfinite(value)
                or value < 0
                or (header[index] != "volume" and value == 0)
            ):
                raise PreflightError(
                    "invalid_bar",
                    "OHLC prices must be positive; volume nonnegative and finite",
                    listing_id=listing_id,
                )
        if day < window.start:
            previous.append((day, row))
        else:
            selected.append((day, row))
    if not selected:
        raise PreflightError(
            "empty_evaluation_window",
            "No bars exist in the requested [start, end) window; choose a covered interval",
            listing_id=listing_id,
        )
    if len(previous) < warmup_sessions:
        raise PreflightError(
            "insufficient_warmup",
            f"Listing {listing_id} needs {warmup_sessions} previous available sessions but has {len(previous)}; import earlier history or move the evaluation start",
            listing_id=listing_id,
            details={
                "required": warmup_sessions,
                "available": len(previous),
                "before": window.start.isoformat(),
            },
        )
    warnings = []
    if expected_sessions is None:
        warnings.append(
            "Exchange-session completeness was not checked: no pinned calendar schedule was supplied."
        )
    else:
        if len(expected_sessions) > limits.max_rows:
            raise PreflightError(
                "calendar_budget", "Pinned calendar exceeds the row budget"
            )
        expected = {
            _day(day)
            for day in expected_sessions
            if window.start <= _day(day) < window.end
        }
        observed = {day for day, _ in selected}
        if not expected:
            raise PreflightError(
                "missing_calendar_evidence",
                "Pinned calendar contains no evaluation sessions",
                listing_id=listing_id,
            )
        missing, unexpected = expected - observed, observed - expected
        if missing or unexpected:
            raise PreflightError(
                "session_coverage_gap",
                "Evaluation bars disagree with the pinned session schedule; resolve missing/non-session bars in a new snapshot",
                listing_id=listing_id,
                details={
                    "missing_sessions": [str(d) for d in sorted(missing)],
                    "unexpected_sessions": [str(d) for d in sorted(unexpected)],
                },
            )
    if history_policy == "source_cold_start":
        warnings.append(COLD_START_WARNING)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(header)
    for _, row in (*previous, *selected):
        writer.writerow(row)
    content = output.getvalue().encode()
    if len(content) > limits.max_file_bytes:
        raise PreflightError(
            "csv_byte_budget", "Staged CSV exceeds the per-file byte budget"
        )
    return StagedCsv(
        content,
        CsvWindowReport(
            listing_id,
            window,
            warmup_sessions,
            len(previous),
            len(selected),
            previous[0][0] if previous else selected[0][0],
            selected[-1][0],
            source_digest,
            hashlib.sha256(content).hexdigest(),
            "pinned_schedule" if expected_sessions is not None else "unavailable",
            tuple(warnings),
        ),
    )


def preflight_csv_window(data: bytes, **kwargs) -> CsvWindowReport:
    return stage_csv_window(data, **kwargs).report


def _file_path(name: str):
    if not isinstance(name, str):
        raise PreflightError("unsupported_input_path", "Manifest path must be a string")
    path = PurePosixPath(name)
    if (
        not isinstance(name, str)
        or "\\" in name
        or str(path) != name
        or path.is_absolute()
        or len(path.parts) != 2
        or path.parts[0] not in ("data", "universe")
        or path.suffix != ".csv"
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.csv", path.name)
    ):
        raise PreflightError(
            "unsupported_input_path",
            "Manifest contains an unsupported evaluation input path",
        )
    return path


def _universe_files(
    files: Mapping[str, bytes], window: DateWindow, limits: CsvLimits, state: str
):
    snapshots: dict[date, list[list[str]]] = {}
    for name, content in files.items():
        if not name.startswith("universe/"):
            continue
        rows = _rows(content, limits)
        header = next(rows)
        if not {"updateDate", "code", "code_name"}.issubset(header):
            raise PreflightError(
                "invalid_universe", "Universe CSV needs updateDate, code and code_name"
            )
        records = list(rows)
        days = {_day(row[header.index("updateDate")]) for row in records}
        if len(days) != 1:
            raise PreflightError(
                "ambiguous_universe_snapshot",
                "Universe file must declare one unambiguous snapshot date; republish normalized membership",
            )
        day = next(iter(days))
        if day >= window.end:
            continue
        normalized = [
            [str(day), row[header.index("code")], row[header.index("code_name")]]
            for row in records
        ]
        if day in snapshots:
            raise PreflightError(
                "duplicate_universe_snapshot",
                "Multiple universe files have the same effective date",
            )
        snapshots[day] = normalized
    if state in {"user_fixed", "current_snapshot"}:
        members = {
            frozenset(row[1] for row in records) for records in snapshots.values()
        }
        if len(members) > 1:
            raise PreflightError(
                "inconsistent_fixed_universe",
                "Fixed/current-snapshot membership must not change across effective dates; publish historical membership instead",
            )
    before = [day for day in snapshots if day <= window.start]
    if not before:
        raise PreflightError(
            "missing_universe_snapshot",
            "No universe snapshot is effective at evaluation start",
        )
    first = max(before)
    selected = sorted(
        day for day in snapshots if day == first or window.start < day < window.end
    )
    output, symbols = {}, set()
    for day in selected:
        records = snapshots[day]
        codes = [row[1] for row in records]
        if len(set(codes)) != len(codes):
            raise PreflightError(
                "duplicate_universe_member",
                "Universe snapshot contains duplicate members",
            )
        symbols.update(codes)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["updateDate", "code", "code_name"])
        writer.writerows(records)
        output[f"universe/members_list_{day}.csv"] = stream.getvalue().encode()
    return output, symbols


def stage_dataset_window(
    config: RunConfig,
    split: Split,
    manifest: Mapping,
    read_file: Callable[[str], bytes],
    *,
    limits: CsvLimits = DEFAULT_LIMITS,
    evaluation_window: DateWindow | Mapping | None = None,
) -> StagedDataset:
    """Stage one split only; caller verifies DB ownership and strategy/reference IDs.

    read_file receives a validated relative manifest path. Its implementation must
    enforce storage containment and bounded reads (including symlink protection).
    Files are rehashed here, so a prior API preflight is not trusted at execution.
    """
    # Revalidate even a Pydantic instance constructed via unchecked model_copy.
    config = RunConfig.model_validate(config.model_dump(mode="json"))
    if config.execution_mode == "optimization" and split == "holdout":
        raise PreflightError(
            "holdout_not_authorized",
            "Optimization runs cannot stage holdout inputs; use a separately authorized final evaluation",
        )
    window = config.windows.get(split)
    if evaluation_window is not None:
        value = (
            evaluation_window.model_dump(mode="json")
            if isinstance(evaluation_window, DateWindow)
            else dict(evaluation_window)
        )
        if "split" in value and value.pop("split") != split:
            raise PreflightError(
                "unregistered_evaluation_window",
                "Evaluation split does not match the registered window",
            )
        proposed = DateWindow.model_validate(value)
        record = {
            "split": split,
            "start": str(proposed.start),
            "end": str(proposed.end),
        }
        if record not in evaluation_windows(config):
            raise PreflightError(
                "unregistered_evaluation_window",
                "Optimizer evaluation must use an exact registered calendar-year train or whole validation window; holdout is excluded",
            )
        window = proposed
    if hashlib.sha256(canonical_bytes(manifest)).hexdigest() != config.dataset.sha256:
        raise PreflightError(
            "manifest_hash_mismatch", "Immutable dataset manifest changed"
        )
    universe = manifest.get("universe") or {}
    state = universe.get("state", manifest.get("universe_status"))
    if state not in {
        "user_fixed",
        "current_snapshot",
        "historical_membership",
        "point_in_time_verified",
    }:
        raise PreflightError(
            "unknown_universe_semantics",
            "Snapshot must explicitly declare universe membership semantics",
        )
    for key, value in (
        ("id", config.universe.id),
        ("content_sha256", config.universe.sha256),
    ):
        if universe.get(key) is not None and universe[key] != value:
            raise PreflightError(
                "universe_reference_mismatch",
                "Dataset universe differs from the immutable RunConfig",
            )
    if state == "current_snapshot":
        as_of = universe.get("as_of")
        if not isinstance(as_of, str) or window.start < _day(as_of[:10]):
            raise PreflightError(
                "universe_not_historical",
                "Current snapshot membership cannot be back-applied before its as-of date",
            )
    compatibility = manifest.get("evaluation_compatibility") or {}
    if compatibility.get("compatible") is False or (manifest.get("quality") or {}).get(
        "unknown_flags"
    ):
        raise PreflightError(
            "incompatible_data_semantics",
            "Resolve dataset units and unknown semantic flags in a new snapshot",
        )
    entries = manifest.get("files")
    if not isinstance(entries, dict) or not entries or len(entries) > limits.max_files:
        raise PreflightError(
            "file_budget", "Manifest requires a bounded nonempty evaluation file set"
        )
    contents, total = {}, 0
    for name, digest in sorted(entries.items()):
        _file_path(name)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PreflightError(
                "invalid_file_hash", "Each input file requires a SHA-256 digest"
            )
        content = read_file(name)
        if not isinstance(content, bytes) or len(content) > limits.max_file_bytes:
            raise PreflightError(
                "csv_byte_budget", "CSV exceeds the per-file byte budget"
            )
        total += len(content)
        if total > limits.max_total_bytes:
            raise PreflightError(
                "dataset_byte_budget", "Dataset exceeds the total input byte budget"
            )
        _verify(content, digest)
        contents[name] = content
    staged_universe, active_symbols = _universe_files(contents, window, limits, state)
    mapping = manifest.get("evaluator_symbol_mapping")
    if (
        not isinstance(mapping, dict)
        or not mapping
        or len(set(mapping.values())) != len(mapping)
    ):
        raise PreflightError(
            "missing_listing_mapping",
            "Research staging requires an exact listing-to-evaluator-symbol mapping",
        )
    if not active_symbols.issubset(set(mapping.values())):
        raise PreflightError(
            "unknown_universe_member",
            "Universe references a symbol absent from the pinned listing mapping",
        )
    if config.task.endswith("_time") and len(active_symbols) != 1:
        raise PreflightError(
            "single_listing_required", "Timing seed requires exactly one active listing"
        )
    coverage = {entry["listing_id"]: entry for entry in manifest.get("coverage", [])}
    reports, output = [], dict(staged_universe)
    warnings = [
        "Native code shares the engine process; staged inputs do not prove absence of strategy look-ahead.",
        "Pinned universe state is not independently verified point-in-time evidence.",
    ]
    for listing in manifest.get("listings", []):
        if listing.get("calendar_note"):
            warnings.append(listing["calendar_note"])
    for listing_id, symbol in sorted(mapping.items()):
        if symbol not in active_symbols:
            continue  # Never expose holdout-only/future constituent data files.
        name = f"data/{symbol}.csv"
        if name not in contents:
            raise PreflightError(
                "missing_listing_data",
                "A required listing CSV is missing",
                listing_id=listing_id,
            )
        evidence = coverage.get(listing_id, {})
        schedule = evidence.get("expected_sessions") or None
        staged = stage_csv_window(
            contents[name],
            listing_id=listing_id,
            window=window,
            warmup_sessions=config.warmup.sessions,
            history_policy=config.warmup.history_policy,
            expected_sha256=entries[name],
            expected_sessions=schedule,
            limits=limits,
        )
        reports.append(staged.report)
        if config.task.endswith("_time"):
            output["data/INDEX.csv"] = staged.content
            warnings.append(
                f"Timing staging aliases {name} to data/INDEX.csv; the source listing identity and input hash remain pinned. INDEX is an evaluator alias, not an index or benchmark classification."
            )
        else:
            output[name] = staged.content
        warnings.extend(staged.report.warnings)
    if not reports:
        raise PreflightError(
            "empty_evaluation_window", "No active listing can be staged"
        )
    return StagedDataset(
        output,
        DatasetWindowReport(
            split, window, tuple(reports), tuple(dict.fromkeys(warnings)), state
        ),
    )


def preflight_dataset(
    config: RunConfig,
    split: Split,
    manifest: Mapping,
    read_file: Callable[[str], bytes],
    *,
    limits: CsvLimits = DEFAULT_LIMITS,
    evaluation_window: DateWindow | Mapping | None = None,
) -> DatasetWindowReport:
    return stage_dataset_window(
        config,
        split,
        manifest,
        read_file,
        limits=limits,
        evaluation_window=evaluation_window,
    ).report
