"""Pure portable staging from pinned normalized data and exchange-clock sidecars.

No calendar/provider/array dependency is imported. Missing clocks, uncertain
corporate actions and bar gaps reject rather than being inferred or filled.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date

from packages.strategy_sdk import (
    BarView,
    JsonLimits,
    ListingView,
    canonical_json,
    hash_json,
    parse_json,
)
from packages.strategy_sdk.base import utc_timestamp

from .config import DateWindow, canonical_bytes
from .portable_config import PortableRunConfig, portable_evaluation_windows
from .windows import PreflightError

DATA_LIMITS = JsonLimits(
    max_bytes=32 * 1024 * 1024, max_nodes=2_000_000, max_items=100_000
)
SCHEDULE_VERSION = "marketdata.session-schedule.v1"
CALENDAR_VERSION = "4.13.2"


@dataclass(frozen=True)
class PortableWindowReport:
    split: str
    window: DateWindow
    staged_start: date
    staged_last: date
    warmup_sessions: int
    active_sessions: int
    listing_count: int
    market_data_sha256: str
    universe_sha256: str
    actions_sha256: str
    session_schedule_sha256: str
    action_coverage_sha256: str
    universe_state: str
    warnings: tuple[str, ...]
    point_in_time_proven: bool = False
    is_synthetic: bool = False


@dataclass(frozen=True)
class PortableStagedDataset:
    market: Mapping
    content: bytes
    report: PortableWindowReport


def _fail(code, text):
    raise PreflightError(code, text)


def _window(config, split, evaluation_window):
    config = PortableRunConfig.model_validate_json(config.canonical_bytes())
    if split not in {"train", "validation"}:
        _fail("holdout_forbidden", "Portable research never admits holdout")
    permitted = portable_evaluation_windows(config)
    if evaluation_window is None:
        choices = [v for v in permitted if v["split"] == split]
        if len(choices) != 1:
            _fail(
                "evaluation_window_required",
                "Specify one canonical yearly training window",
            )
        value = choices[0]
    elif isinstance(evaluation_window, DateWindow):
        value = {"split": split, **evaluation_window.model_dump(mode="json")}
    else:
        value = dict(evaluation_window)
        if set(value) != {"split", "start", "end"}:
            _fail(
                "invalid_evaluation_window", "Expected canonical split/start/end window"
            )
    if value not in permitted or value["split"] != split:
        _fail(
            "evaluation_window_forbidden",
            "Window is not an admitted train/validation engine run",
        )
    return config, DateWindow.model_validate({k: value[k] for k in ("start", "end")})


def stage_portable_dataset_window(
    config: PortableRunConfig,
    split: str,
    manifest: Mapping,
    read_file: Callable[[str], bytes],
    *,
    evaluation_window=None,
) -> PortableStagedDataset:
    config, window = _window(config, split, evaluation_window)
    return _stage_portable_window(config, split, manifest, read_file, window=window)


def _stage_portable_window(
    config: PortableRunConfig,
    split: str,
    manifest: Mapping,
    read_file: Callable[[str], bytes],
    *,
    window: DateWindow,
) -> PortableStagedDataset:
    """Private data parser, NOT permission to choose or execute an input window.

    Research enters only through the strict public whitelist above. The separate
    final staging entry point must verify its frozen selection and exact holdout
    before calling this parser. No public allow_holdout flag exists.
    """
    config = PortableRunConfig.model_validate_json(config.canonical_bytes())
    window = DateWindow.model_validate(window.model_dump(mode="json"))
    # Dataset identity intentionally retains the existing persisted dataset bytes.
    if hashlib.sha256(canonical_bytes(manifest)).hexdigest() != config.dataset.sha256:
        _fail("manifest_hash_mismatch", "Immutable dataset manifest changed")
    request = manifest.get("request") or {}
    if request.get("interval") != "1d" or not (
        request.get("start", "9999")
        <= str(window.start)
        < str(window.end)
        <= request.get("end", "0000")
    ):
        _fail(
            "unsupported_data_range",
            "Portable daily window lies outside immutable coverage",
        )
    semantics = manifest.get("price_semantics") or {}
    if (semantics.get("split_basis"), semantics.get("dividend_basis")) != (
        "unadjusted",
        "unadjusted",
    ):
        _fail(
            "raw_prices_required",
            "Portable cash profile requires explicitly unadjusted OHLC prices",
        )
    if (manifest.get("evaluation_compatibility") or {}).get("compatible") is False or (
        manifest.get("quality") or {}
    ).get("unknown_flags"):
        _fail(
            "incompatible_data_semantics",
            "Dataset has unresolved unit or quality semantics",
        )
    if any(w.get("severity") == "error" for w in manifest.get("warnings", [])):
        _fail("incompatible_data_semantics", "Dataset carries unresolved errors")
    universe = manifest.get("universe") or {}
    if universe.get("state") != "user_fixed":
        _fail(
            "unsupported_universe_semantics",
            "Initial portable staging supports an explicit user-fixed research basket only; no historical/PIT inference",
        )
    for key, ref in (
        ("id", config.universe.id),
        ("content_sha256", config.universe.sha256),
    ):
        if universe.get(key) is not None and universe[key] != ref:
            _fail(
                "universe_reference_mismatch",
                "Dataset universe differs from immutable config",
            )
    artifacts = manifest.get("artifact_files") or {}
    loaded = {}
    total = 0

    def read(name):
        nonlocal total
        digest = artifacts.get(name)
        if not isinstance(digest, str) or len(digest) != 64:
            _fail(
                "missing_portable_evidence",
                f"Pinned {name} is required; legacy snapshots must be republished",
            )
        raw = read_file(name)
        if type(raw) is not bytes or len(raw) > DATA_LIMITS.max_bytes:
            _fail("dataset_byte_budget", "Portable artifact exceeds bounded input size")
        total += len(raw)
        if total > 64 * 1024 * 1024:
            _fail("dataset_byte_budget", "Portable input exceeds total read budget")
        if hashlib.sha256(raw).hexdigest() != digest:
            _fail("input_hash_mismatch", "Immutable portable evidence bytes changed")
        loaded[name] = digest
        return parse_json(raw, limits=DATA_LIMITS)

    schedule = read("sessions.json")
    if (
        not isinstance(schedule, dict)
        or schedule.get("schema_version") != SCHEDULE_VERSION
        or schedule.get("generator")
        != {"name": "exchange_calendars", "version": CALENDAR_VERSION}
    ):
        _fail(
            "unsupported_session_schedule",
            "Pinned reviewed exchange calendar version is required",
        )
    if (schedule.get("start"), schedule.get("end")) != (
        request["start"],
        request["end"],
    ):
        _fail(
            "session_schedule_range",
            "Session schedule does not cover the immutable requested range",
        )
    calendar_name = "XSHG" if config.market == "CN" else "XNYS"
    selected_calendar = (schedule.get("calendars") or {}).get(calendar_name) or {}
    if selected_calendar.get("status") != "complete":
        _fail(
            "missing_session_clocks",
            "Actual exchange session clocks are unavailable; no inference from bars",
        )
    source_sessions = selected_calendar.get("sessions")
    if not isinstance(source_sessions, list) or not 1 <= len(source_sessions) <= 5000:
        _fail("session_budget", "Exchange schedule is empty or oversized")
    previous = None
    for session in source_sessions:
        if not isinstance(session, dict) or set(session) != {
            "session_date",
            "open_time",
            "close_time",
            "next_open_time",
        }:
            _fail("invalid_session_clock", "Exchange session has unsupported fields")
        day = date.fromisoformat(session["session_date"])
        opening, closing, following = (
            utc_timestamp(session[k])
            for k in ("open_time", "close_time", "next_open_time")
        )
        if (
            not opening < closing < following
            or not request["start"] <= str(day) < request["end"]
        ):
            _fail("invalid_session_clock", "Invalid exchange session times or range")
        if previous and (
            previous["session_date"] >= str(day)
            or previous["next_open_time"] != session["open_time"]
        ):
            _fail(
                "session_schedule_gap",
                "Pinned session sequence is unordered or skips its recorded next open",
            )
        previous = session
    prior = [s for s in source_sessions if s["session_date"] < str(window.start)]
    active = [
        s
        for s in source_sessions
        if str(window.start) <= s["session_date"] < str(window.end)
    ]
    if len(prior) < config.warmup.sessions or not active:
        _fail(
            "insufficient_warmup",
            "Require complete pre-window warmup and a nonempty active schedule",
        )
    sessions = prior[-config.warmup.sessions :] + active
    session_map = {s["session_date"]: s for s in sessions}
    coverage = read("corporate-action-coverage.json")
    if (
        not isinstance(coverage, dict)
        or coverage.get("schema_version") != "marketdata.corporate-action-coverage.v1"
        or (coverage.get("start"), coverage.get("end"))
        != (request["start"], request["end"])
    ):
        _fail(
            "unknown_corporate_actions",
            "Pinned corporate-action coverage must span the requested data",
        )
    actions = read("actions.json")
    if not isinstance(actions, list):
        _fail(
            "unknown_corporate_actions",
            "Corporate actions must be an explicitly covered array",
        )
    for action in actions:
        if not isinstance(action, dict) or action.get("action_type") not in {
            "dividend",
            "split",
            "capital_gain",
        }:
            _fail("unknown_corporate_actions", "Unknown corporate action type")
        if (
            sessions[0]["session_date"]
            <= action.get("session_date", "")
            < str(window.end)
        ):
            _fail(
                "corporate_actions_unsupported",
                "Portable profile rejects warmup/active windows containing corporate actions",
            )
    listings, warnings = (
        [],
        [
            "User-fixed basket is a retrospective research assumption, not point-in-time listing or membership evidence.",
            "Bar availability at exchange close is a daily research timing assumption, not verified historical dissemination latency.",
            "Exchange clocks are checked again independently inside the isolated engine.",
        ],
    )
    source_listings = manifest.get("listings")
    if not isinstance(source_listings, list) or not 1 <= len(source_listings) <= 100:
        _fail("listing_budget", "Require a bounded nonempty listing set")
    if config.strategy_kind == "event" and len(source_listings) != 1:
        _fail(
            "single_listing_required",
            "Initial portable event profile is single-listing only",
        )
    ids = [v.get("listing_id") for v in source_listings]
    if len(set(ids)) != len(ids):
        _fail("duplicate_listing", "Duplicate immutable listing identity")
    if universe.get("members") is not None and set(ids) != {
        m.get("listing_id") for m in universe["members"]
    }:
        _fail(
            "universe_reference_mismatch", "Fixed universe and dataset listings differ"
        )
    action_records = coverage.get("listings")
    if (
        not isinstance(action_records, list)
        or len(action_records) != len(ids)
        or {r.get("listing_id") for r in action_records} != set(ids)
    ):
        _fail("unknown_corporate_actions", "Action evidence must cover every listing")
    if (
        any(
            r.get("status") != "known_none"
            or r.get("evidence") != "synthetic_by_construction"
            or not r.get("provenance_ids")
            for r in action_records
        )
        or coverage.get("is_synthetic") is not True
        or coverage.get("generator_version") != "portable.synthetic.daily.v1"
    ):
        _fail(
            "unknown_corporate_actions",
            "Initial portable action coverage admits only explicit no-actions synthetic evidence, never an unexplained empty array",
        )
    if semantics.get("algorithm_version") != coverage["generator_version"]:
        _fail("unknown_corporate_actions", "Price generator and action evidence differ")
    sources = {p.get("provenance_id"): p for p in manifest.get("provenance", [])}
    for record in action_records:
        intervals = []
        for source_id in record["provenance_ids"]:
            p = sources.get(source_id) or {}
            if (
                p.get("connector") != "portable_fixture"
                or p.get("provider") != "synthetic"
                or p.get("evidence") != "fixture"
                or p.get("endpoint") != coverage["generator_version"]
                or (p.get("requested_params") or {}).get("listing_id")
                != record["listing_id"]
                or p.get("effective_params")
                != {
                    "is_synthetic": True,
                    "price_basis": "unadjusted",
                    "corporate_actions": "none_by_construction",
                    "generator_version": coverage["generator_version"],
                }
            ):
                _fail(
                    "unknown_corporate_actions",
                    "Synthetic no-actions evidence lacks matching provenance",
                )
            params = p["requested_params"]
            start, end = params.get("start"), params.get("end")
            if not isinstance(start, str) or not isinstance(end, str):
                _fail(
                    "unknown_corporate_actions",
                    "Action provenance lacks exact coverage bounds",
                )
            # Parse dates before comparing, rather than trusting lexical payloads.
            start, end = str(date.fromisoformat(start)), str(date.fromisoformat(end))
            intervals.append((start, end))
        cursor = request["start"]
        for start, end in sorted(intervals):
            if start > cursor or end <= start:
                _fail(
                    "unknown_corporate_actions",
                    "Action provenance has an uncovered interval",
                )
            cursor = max(cursor, end)
        if cursor < request["end"]:
            _fail(
                "unknown_corporate_actions",
                "Action provenance does not cover the requested range",
            )
    warnings.append(
        "Artificial raw-price fixture with no corporate actions by construction; not captured or live market observations."
    )
    for source in sorted(source_listings, key=lambda v: v["listing_id"]):
        binding = (schedule.get("listing_calendars") or {}).get(
            source["listing_id"]
        ) or {}
        proxy = source["mic"] != calendar_name
        if (
            binding.get("calendar") != calendar_name
            or binding.get("mic") != source["mic"]
            or binding.get("calendar_basis")
            != ("declared_proxy" if proxy else "exchange_calendar")
        ):
            _fail(
                "listing_calendar_mismatch",
                "Listing requires an explicitly pinned exchange or declared proxy calendar",
            )
        if proxy:
            if not binding.get("note"):
                _fail(
                    "undeclared_calendar_proxy",
                    "Calendar proxy must have an explicit caveat",
                )
            warnings.append(binding["note"])
        if (
            source.get("asset_type") != "equity"
            or source.get("delisted_on") is not None
            or (
                source.get("listed_on")
                and source["listed_on"] > sessions[0]["session_date"]
            )
        ):
            _fail(
                "unsupported_listing_lifetime",
                "Initial fixed-basket portable profile requires equities effective for the complete staged range",
            )
        value = {
            key: source[key]
            for key in (
                "listing_id",
                "instrument_id",
                "symbol",
                "mic",
                "currency",
                "timezone",
            )
        }
        value.update(
            calendar=calendar_name,
            effective_from=request["start"],
            effective_to=None,
            effective_to_known_at=None,
            known_at=f"{request['start']}T00:00:00Z",
            lot_size=100 if config.market == "CN" else 1,
        )
        listing = ListingView.model_validate_json(canonical_json(value))
        if listing.currency != ("CNY" if config.market == "CN" else "USD"):
            _fail("listing_market_mismatch", "Listing currency differs from run market")
        listings.append(listing.model_dump(mode="json"))
    raw_bars = read("preview.json")
    if not isinstance(raw_bars, list) or not raw_bars:
        _fail("empty_evaluation_window", "Normalized bars are empty")
    bars, seen = [], set()
    for source in raw_bars:
        if not isinstance(source, dict) or source.get("listing_id") not in ids:
            _fail("unknown_bar_listing", "Normalized bar has unknown listing identity")
        key = (source["listing_id"], source.get("session_date"))
        if key in seen:
            _fail("duplicate_bar", "Duplicate normalized listing/session")
        seen.add(key)
        if source.get("session_date") not in session_map:
            continue
        clock = session_map[source["session_date"]]
        if (
            source.get("volume_unit") != "shares"
            or source.get("currency") != ("CNY" if config.market == "CN" else "USD")
            or source.get("flags")
        ):
            _fail(
                "unsupported_bar_semantics",
                "Require clean known share volume, currency and raw OHLC",
            )
        value = {
            k: source[k]
            for k in (
                "listing_id",
                "session_date",
                "open",
                "high",
                "low",
                "close",
                "volume",
            )
        }
        value.update(
            completed_at=clock["close_time"],
            available_at=clock["close_time"],
            volume_unit="shares",
            complete=True,
        )
        bars.append(
            BarView.model_validate_json(canonical_json(value)).model_dump(mode="json")
        )
    expected = {(listing_id, s["session_date"]) for listing_id in ids for s in sessions}
    if {(b["listing_id"], b["session_date"]) for b in bars} != expected:
        _fail(
            "session_coverage_gap",
            "Every expected listing needs every staged warmup/active bar; missing or suspended bars are never filled or dropped",
        )
    bars.sort(key=lambda b: (b["session_date"], b["listing_id"]))
    market = {
        "listings": listings,
        "bars": bars,
        "sessions": sessions,
        "corporate_actions": [],
    }
    content = canonical_json(market, limits=DATA_LIMITS)
    report = PortableWindowReport(
        split,
        window,
        date.fromisoformat(sessions[0]["session_date"]),
        date.fromisoformat(sessions[-1]["session_date"]),
        config.warmup.sessions,
        len(active),
        len(ids),
        hashlib.sha256(content).hexdigest(),
        hash_json(listings),
        hash_json([]),
        loaded["sessions.json"],
        loaded["corporate-action-coverage.json"],
        "user_fixed",
        tuple(dict.fromkeys(warnings)),
        False,
        True,
    )
    return PortableStagedDataset(market, content, report)


def preflight_portable_dataset(
    config, split, manifest, read_file, *, evaluation_window=None
):
    return stage_portable_dataset_window(
        config, split, manifest, read_file, evaluation_window=evaluation_window
    ).report
