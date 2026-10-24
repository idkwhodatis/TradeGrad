"""Immutable, non-causal two-leaf comparison of admitted persisted evidence.

No strategy is imported, evaluated or explained by a model. DuckDB pairs source
observations and reads projected Parquet; bounded semantic multisets retain all
ordinals, missing states and explicit execution graph evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import replace
from uuid import UUID

import duckdb

from .comparison_contracts import (
    ALGORITHM_VERSION,
    COMMON_CONFIG_FIELDS,
    DEFAULT_LIMITS,
    FIELD_TOLERANCES,
    READABLE_ALGORITHM_VERSIONS,
    SECTIONS,
    TOLERANCE_VERSION,
    ComparisonError,
    ComparisonInput,
    ComparisonManifest,
    ComparisonPublication,
    ComparisonSectionSummary,
    ComparisonSidePins,
    ConfigurationDifference,
    MechanismChange,
)
from .comparison_semantics import equal_value, group_record
from .comparison_storage import (
    ComparisonControl,
    ComparisonReader,
    ComparisonWriter,
    TrustedTraceScan,
    check_file_budget,
)
from .contracts import canonical_bytes, digest
from .storage import TraceStorageError

__all__ = [
    "ComparisonControl",
    "ComparisonReader",
    "compare_evaluations",
    "comparison_input_hash",
    "comparison_side_pins",
]
PAIR_FIELDS = ["data_event_id", "callback_phase", "scope", "event_seq", "instrument_id"]
_DEFINITIONS = {
    "events": "Full outer source-event pairing, never date-only or random-ID pairing; both exact decision pre-states are retained. Execution-result snapshots are separate from decision callbacks.",
    "candidates": "Saved candidate selection/eligibility/rank/score observations, grouped by stage and instrument as ordinal-preserving multisets. Missing rows are not automatically non-selection.",
    "features": "Saved feature IDs and values, validity and cutoff, preserving duplicate IDs and source ordinals. Uncollected features are not evaluated again.",
    "predicates": "Saved predicate IDs, operands and evaluation states, preserving duplicate IDs and source ordinals. False is distinct from not evaluated, unknown and not sampled.",
    "targets": "Raw targets and constrained plans compared separately. Omission/preserve is not an explicit zero target or liquidation; targets are not actual fills.",
    "orders": "Actual admitted economic orders and explicit decision/logical/child ID graph. Cross-run equality uses a documented semantic projection with graph topology, retaining every original ID and duplicate multiplicity.",
    "fills": "Actual individual fill rows, explicitly linked to their local orders. These are not closed trades; missing fills do not establish missing order intent.",
    "positions": "Actual persisted position snapshots at exact observed timestamps. Missing rows are unknown, never synthesized flat positions; path differences are not isolated rule effects.",
    "account": "Actual persisted account snapshots at exact observed timestamps and per-observation deltas, not interpolated daily values or a synthetic initial-capital row.",
    "turnover": "Gross observed-window turnover = sum(abs(fill quantity × fill price)) / arithmetic mean of positive observed account equity, over the stated window. Not annualized, not one-way turnover, and not certified complete-session coverage.",
    "holding_duration": "Actual closed-trade entry-to-exit elapsed seconds where retained; open positions are right-censored at the last observed account timestamp and unknown entry dates/durations remain null.",
}


def _snapshot(value, limits):
    if not isinstance(value, ComparisonInput):
        raise ComparisonError()
    try:
        # Freeze mutable nested model/list values before validation and work.
        value = replace(
            value,
            trace_manifest=type(value.trace_manifest).model_validate_json(
                canonical_bytes(value.trace_manifest)
            )
            if value.trace_manifest is not None
            else None,
            spec=type(value.spec).model_validate_json(canonical_bytes(value.spec))
            if value.spec is not None
            else None,
            namespace=type(value.namespace).model_validate_json(
                canonical_bytes(value.namespace)
            )
            if value.namespace is not None
            else None,
        )
        if value.source_kind not in {"strategy_version", "candidate"} or not isinstance(
            value.source_id, UUID
        ):
            raise ComparisonError()
        if (
            not isinstance(value.source, str)
            or not 0 < len(value.source.encode()) <= 1024**2
        ):
            raise ComparisonError()
        if (
            hashlib.sha256(value.source.encode()).hexdigest()
            != value.binding.code_sha256
        ):
            raise ComparisonError()
        if (value.spec is None) != (value.spec_sha256 is None):
            raise ComparisonError()
        if value.spec is not None and (
            value.spec.code_sha256 != value.binding.code_sha256
            or digest(value.spec) != value.spec_sha256
        ):
            raise ComparisonError()
        if (value.trace_manifest is None) != (value.trace_manifest_sha256 is None):
            raise ComparisonError()
        if value.trace_manifest is not None and (
            value.trace_manifest.binding != value.binding
            or digest(value.trace_manifest) != value.trace_manifest_sha256
        ):
            raise ComparisonError()
        if value.trace_manifest is None and value.trace_paths:
            raise ComparisonError()
        if value.namespace is not None:
            namespace, binding = value.namespace, value.binding
            if any(
                getattr(namespace, key) != getattr(binding, key)
                for key in (
                    "dataset_version_id",
                    "dataset_sha256",
                    "universe_version_id",
                    "universe_sha256",
                )
            ):
                raise ComparisonError()
        common = canonical_bytes(dict(value.common_config))
        bundle = canonical_bytes(dict(value.economic_bundle))
        if len(common) > 256 * 1024 or len(bundle) > limits.max_bundle_bytes:
            raise ComparisonError(
                "comparison_capacity",
                "Comparison metadata or economic bundle exceeds its fixed byte budget",
            )
        return replace(
            value,
            common_config=json.loads(common),
            economic_bundle=json.loads(bundle),
            trace_paths=dict(value.trace_paths),
        )
    except ComparisonError:
        raise
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ComparisonError() from None


def _pins(value):
    trace = value.trace_manifest
    return ComparisonSidePins(
        binding=value.binding,
        source_kind=value.source_kind,
        source_id=value.source_id,
        window=value.window,
        spec_revision=value.spec.revision if value.spec else None,
        spec_sha256=value.spec_sha256,
        trace_manifest_sha256=value.trace_manifest_sha256,
        trace_state="historical_absence"
        if trace is None
        else "registered_off"
        if trace.capture.mode == "off"
        else "registered",
        trace_coverage=trace.coverage if trace else None,
        capture=trace.capture if trace else None,
        trace_missing_reasons=list(trace.missing_reasons)
        if trace
        else [
            "This immutable evaluation has no registered structured trace; no decisions were reconstructed."
        ],
        common_config_sha256=digest(value.common_config),
        economic_bundle_sha256=digest(value.economic_bundle),
        namespace=value.namespace,
    )


def comparison_side_pins(value, *, limits=DEFAULT_LIMITS):
    """Public exact side-pin projection for fenced business-layer validation."""
    return _pins(_snapshot(value, limits))


def comparison_input_hash(
    left, right, *, limits=DEFAULT_LIMITS, algorithm_version: str = ALGORITHM_VERSION
):
    """Hash a current request or explicitly validate a supported persisted identity.

    Selecting a legacy version here never enables legacy recomputation. New
    comparisons always use the current algorithm; readers must retain the saved
    manifest's original version instead of silently upgrading its identity.
    """
    if (
        type(algorithm_version) is not str
        or algorithm_version not in READABLE_ALGORITHM_VERSIONS
    ):
        raise ComparisonError(
            "comparison_integrity", "Unsupported comparison algorithm"
        )
    left, right = _snapshot(left, limits), _snapshot(right, limits)
    return digest(
        {
            "left": _pins(left).model_dump(mode="json"),
            "right": _pins(right).model_dump(mode="json"),
            "algorithm": algorithm_version,
            "tolerance_version": TOLERANCE_VERSION,
            "tolerances": FIELD_TOLERANCES,
            "limits": limits.model_dump(mode="json"),
        }
    )


def _configuration(left, right):
    output = []
    missing = object()

    def visit(field, a, b):
        if isinstance(a, dict) and isinstance(b, dict) and (a or b):
            for key in sorted(a.keys() | b.keys()):
                visit(f"{field}.{key}", a.get(key, missing), b.get(key, missing))
        else:
            lp, rp = a is not missing, b is not missing
            output.append(
                ConfigurationDifference(
                    field=field,
                    left_present=lp,
                    right_present=rp,
                    left=a if lp else None,
                    right=b if rp else None,
                    status="unknown"
                    if not lp or not rp
                    else "equal"
                    if canonical_bytes(a) == canonical_bytes(b)
                    else "different",
                )
            )

    for field in sorted(
        set(COMMON_CONFIG_FIELDS)
        | left.common_config.keys()
        | right.common_config.keys()
    ):
        visit(
            field,
            left.common_config.get(field, missing),
            right.common_config.get(field, missing),
        )
    if len(output) > 4096:
        raise ComparisonError(
            "comparison_capacity",
            "Common configuration exceeds its fixed field-difference budget",
        )
    return output


# Reviewed direct parameter use in the two exact maintained template stages.
# This descriptive map is not executable logic and is never applied to arbitrary
# edited source or merely matching inferred rule names.
_CONTROLLED_PARAMETERS = {
    "daily_timing": {
        "features": {"fast_window", "slow_window"},
        "signal": {"exit_buffer"},
        "liquidity": {"liquidity_enabled", "min_average_dollar_volume"},
        "allocation": {"target_weight", "max_weight", "cash_reserve"},
        "execution": set(),
    },
    "cross_sectional": {
        "universe": set(),
        "features": {"lookback"},
        "liquidity": {"lookback", "liquidity_enabled", "min_average_dollar_volume"},
        "ranking": {
            "liquidity_enabled",
            "top_n",
            "retention_rank_buffer",
            "max_positions",
        },
        "allocation": {"gross_target", "top_n", "max_weight", "cash_reserve"},
        "execution": set(),
    },
}


def _mechanisms(left, right):
    category = {
        "universe": "input",
        "input": "input",
        "feature": "input",
        "filter": "filter",
        "ranking": "scoring",
        "allocation": "portfolio",
        "exit": "portfolio",
        "execution": "execution",
    }
    ls = {row.rule_id: row for row in left.spec.rules} if left.spec else {}
    rs = {row.rule_id: row for row in right.spec.rules} if right.spec else {}
    result = []
    for key in sorted(ls.keys() | rs.keys()):
        a, b = ls.get(key), rs.get(key)
        controlled = bool(
            a
            and b
            and a.mapping == b.mapping == "controlled"
            and left.spec.template_id is not None
            and left.spec.template_id == right.spec.template_id
        )
        human = bool(a and b and a.mapping == b.mapping == "human_confirmed")
        mapped = controlled or human
        body_a = a.model_dump(mode="json") if a else None
        body_b = b.model_dump(mode="json") if b else None
        compared_a = {k: v for k, v in (body_a or {}).items() if k != "source_ref"}
        compared_b = {k: v for k, v in (body_b or {}).items() if k != "source_ref"}
        if controlled:
            relevant = _CONTROLLED_PARAMETERS[left.spec.template_id].get(key)
            if relevant is None:
                controlled = mapped = False
            else:
                for body in (compared_a, compared_b):
                    body["parameters"] = {
                        name: value
                        for name, value in body["parameters"].items()
                        if name in relevant
                    }
        result.append(
            MechanismChange(
                category=category[(a or b).category],
                rule_id=key,
                status="equal"
                if mapped and canonical_bytes(compared_a) == canonical_bytes(compared_b)
                else "changed"
                if mapped
                else "unmapped"
                if a and b
                else "left_only"
                if a
                else "right_only",
                mapping="controlled"
                if controlled
                else "human_confirmed"
                if human
                else "inferred"
                if (a and a.mapping == "inferred") or (b and b.mapping == "inferred")
                else "unmapped",
                left_source_ref=a.source_ref if a else None,
                right_source_ref=b.source_ref if b else None,
                left={**body_a, "parameters": compared_a["parameters"]}
                if controlled
                else body_a,
                right={**body_b, "parameters": compared_b["parameters"]}
                if controlled
                else body_b,
                reason="Parameter changes are scoped to reviewed direct uses in this exact maintained template stage; downstream behavior may differ separately."
                if controlled
                else None
                if mapped
                else "No confirmed cross-version logical mapping exists; matching rule labels do not establish equivalent mechanisms.",
            )
        )
    return result


def _namespace(left, right):
    if left.namespace is None or right.namespace is None:
        return (
            "unknown",
            "A verified dataset/universe/instrument namespace is unavailable; events remain unpaired.",
        )
    if left.namespace != right.namespace:
        return (
            "different",
            "Dataset, frozen universe or event/instrument namespace differs; events remain unpaired.",
        )
    return (
        "common",
        "Both inputs share exact verified dataset and frozen universe references and the same declared event/instrument namespace.",
    )


def _scope(value, economic=False):
    capture = value.trace_manifest.capture if value.trace_manifest else None
    return {
        "evaluation_window": value.window.model_dump(mode="json"),
        "observation_window": capture.observation_window.model_dump(mode="json")
        if capture and capture.observation_window and not economic
        else value.window.model_dump(mode="json"),
        "stage_ids": list(capture.stage_ids) if capture and not economic else [],
        "instrument_ids": list(capture.instrument_ids)
        if capture and not economic
        else [],
        "capture_mode": capture.mode
        if capture and not economic
        else "economic_bundle"
        if economic
        else None,
        "whole_evaluation_coverage_certified": False,
    }


def _pairable(event):
    key = event["data_event_id"]
    if (
        key.startswith("native_order:")
        or event["callback_phase"] == "native.execution_result"
    ):
        return False
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].*)?", key):
        return False
    try:
        UUID(key)
        return False
    except (ValueError, AttributeError):
        return True


def _event_index(connection, scan, control, limits):
    events, ids, keys = [], set(), set()
    size = 2
    for event in scan.rows(connection, "events"):
        control.check()
        size += len(canonical_bytes(event)) + 1
        if size > limits.max_event_index_bytes or len(events) >= limits.max_events:
            raise ComparisonError(
                "comparison_capacity",
                "Comparison event index exceeds its fixed row/byte budget",
            )
        key = tuple(event[field] for field in PAIR_FIELDS)
        if event["decision_id"] in ids or key in keys:
            raise ComparisonError()
        ids.add(event["decision_id"])
        keys.add(key)
        events.append(event)
    if len(events) != (scan.manifest.decisions_saved if scan.manifest else 0):
        raise ComparisonError()
    return events


def _pair_events(connection, left, right, common, control, limits):
    # A real fixed DuckDB full outer join over the exact five-field source key.
    # Input JSON is server-built scalar event metadata, never user SQL.
    for table, rows in (
        ("comparison_left_events", left),
        ("comparison_right_events", right),
    ):
        connection.execute(
            f"CREATE TEMP TABLE {table} (decision_id VARCHAR, data_event_id VARCHAR, callback_phase VARCHAR, scope VARCHAR, event_seq BIGINT, instrument_id VARCHAR, pairable BOOLEAN)"
        )
        # executemany remains bounded and never flattens detailed candidate rows.
        for start in range(0, len(rows), 1024):
            control.check()
            connection.executemany(
                f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)",
                [
                    [
                        row["decision_id"],
                        *(row[field] for field in PAIR_FIELDS),
                        common and _pairable(row),
                    ]
                    for row in rows[start : start + 1024]
                ],
            )
    predicates = " AND ".join(
        f"l.{field} IS NOT DISTINCT FROM r.{field}" for field in PAIR_FIELDS
    )
    cursor = connection.execute(
        f"SELECT l.decision_id, r.decision_id FROM comparison_left_events l FULL OUTER JOIN comparison_right_events r ON l.pairable AND r.pairable AND {predicates} ORDER BY coalesce(l.data_event_id,r.data_event_id),coalesce(l.callback_phase,r.callback_phase),coalesce(l.event_seq,r.event_seq),coalesce(l.decision_id,r.decision_id)"
    )
    pairs = []
    while batch := cursor.fetchmany(1024):
        control.check()
        pairs.extend(batch)
        if len(pairs) > 2 * limits.max_events:
            raise ComparisonError(
                "comparison_capacity",
                "Comparison event pairing exceeds its fixed row budget",
            )
    return pairs


def _observation_kind(event):
    # A capability is not a pairing key: it only limits claims of absence.
    # Dates, prices and proximity never establish event identity.
    return tuple(event[field] for field in ("callback_phase", "scope", "instrument_id"))


def _observation_capabilities(events, control, limits):
    kinds = {}
    opaque = False
    # Charge the original event metadata and every new key/stage entry against
    # the same fixed index envelope, with conservative container overhead. This
    # is a union of observed keys, never phase/scope/instrument Cartesian products.
    size = 2

    def charge(amount):
        nonlocal size
        size += amount
        if size > limits.max_event_index_bytes:
            raise ComparisonError(
                "comparison_capacity",
                "Comparison observation capabilities exceed the fixed event index budget",
            )

    for event in events:
        control.check()
        charge(len(canonical_bytes(event)) + 1)
        if not _pairable(event):
            opaque = True
        else:
            key = _observation_kind(event)
            if key not in kinds:
                charge(512 + len(canonical_bytes(key)))
                kinds[key] = {}
                for stage in event["stage_ids"]:
                    if stage not in kinds[key]:
                        charge(128 + len(stage.encode("utf-8")))
                        # Two collection bits, not target values or target arrays.
                        kinds[key][stage] = 0
            else:
                # A stage omitted on even one matching-kind callback cannot
                # certify collection on a missing callback. Keep an intersection,
                # with no new entries and no refund of the charged peak storage.
                for stage in tuple(kinds[key]):
                    if stage not in event["stage_ids"]:
                        del kinds[key][stage]
    return kinds, opaque


def _target_collection_capabilities(
    connection, scan, side, capabilities, control, limits
):
    control.check()
    kinds, _ = capabilities
    paths = scan.source("stages")
    if not paths or not kinds:
        return
    # Inspect only collection presence on pinned trusted stage files. Per-kind
    # all-observation intersection keeps even intermittently null targets unknown;
    # an explicit [] is collected, never confused with an omitted/None target.
    largest = connection.execute(
        "SELECT coalesce(max(octet_length(encode(payload))),0) FROM read_parquet(?)",
        [paths],
    ).fetchone()[0]
    if largest > limits.max_record_bytes:
        raise ComparisonError(
            "comparison_capacity", "A stage exceeds the fixed record budget"
        )
    cursor = connection.execute(
        f"""SELECT e.callback_phase, e.scope, e.instrument_id, s.stage_id,
            bool_and(coalesce(json_type(s.payload, '$.raw_targets') = 'ARRAY', false)),
            bool_and(coalesce(json_type(s.payload, '$.constrained_plan') = 'ARRAY', false))
        FROM read_parquet(?) s JOIN comparison_{side}_events e
            ON s.decision_id = e.decision_id
        WHERE e.pairable
        GROUP BY e.callback_phase, e.scope, e.instrument_id, s.stage_id""",
        [paths],
    )
    while batch := cursor.fetchmany(128):
        control.check()
        for phase, scope, instrument, stage, raw, constrained in batch:
            key = (phase, scope, instrument)
            if key not in kinds:
                raise ComparisonError()
            if stage not in kinds[key]:
                # Intermittently omitted stages were deliberately removed from
                # the capability intersection. Exact event validation still runs.
                continue
            # Update only already charged entries; no second capability index.
            kinds[key][stage] = int(raw) | (int(constrained) << 1)


def _missing_state(value, event, capabilities):
    trace = value.trace_manifest
    if trace is None or trace.capture.mode == "off":
        return "not_collected"
    capture = trace.capture
    if (
        trace.decisions_seen != trace.decisions_saved
        or capture.observation_window
        or capture.stage_ids
        or capture.instrument_ids
    ):
        return "incomplete_coverage"
    kinds, opaque = capabilities
    # Retaining every emitted record does not mean the strategy emitted every
    # callback. Generic execution snapshots cannot certify strategy observation
    # capability, including in a mixed source-emitted/execution-only trace.
    if opaque or _observation_kind(event) not in kinds:
        return "incomplete_coverage"
    # A partial linked final-state stage alone is not a missing source event.
    return "observed_absence"


def _missing_section_state(value, event, capabilities, key, table, fallback):
    if fallback != "observed_absence":
        return fallback
    trace = value.trace_manifest
    kinds, _ = capabilities
    if key[0] not in kinds.get(_observation_kind(event), ()) or (
        table in {"features", "predicates"} and trace.capture.mode != "research"
    ):
        return "not_collected"
    if table == "targets":
        bit = 1 if key[2] == "raw_targets" else 2
        if not kinds[_observation_kind(event)][key[0]] & bit:
            return "not_collected"
    if (
        trace.detail_rows_seen != trace.detail_rows_saved
        or trace.stage_coverage.get(key[0]) != "complete"
    ):
        return "incomplete_coverage"
    return "observed_absence"


def _details(connection, scan, event):
    if event is None:
        return None, {}
    headers = list(
        scan.rows(
            connection,
            "decisions",
            decision_id=event["decision_id"],
            day=event["session_date"],
        )
    )
    stages = list(
        scan.rows(
            connection,
            "stages",
            decision_id=event["decision_id"],
            day=event["session_date"],
        )
    )
    if len(headers) != 1:
        raise ComparisonError()
    header = headers[0]["payload"]
    if (
        any(header[field] != event[field] for field in PAIR_FIELDS)
        or header["decision_id"] != event["decision_id"]
    ):
        raise ComparisonError()
    indexed = {row["stage_id"]: row["payload"] for row in stages}
    if len(indexed) != len(stages) or set(indexed) != set(event["stage_ids"]):
        raise ComparisonError()
    header = {
        **header,
        "session_date": event["session_date"],
        "stage_coverage": [
            {
                "stage_id": key,
                "coverage": row["coverage"],
                "missing_reason": row["missing_reason"],
            }
            for key, row in indexed.items()
        ],
    }
    return header, indexed


def _observe_section(connection, scan, event, stages, table):
    if event is None:
        return {}
    rows = list(
        scan.rows(
            connection,
            table,
            decision_id=event["decision_id"],
            day=event["session_date"],
            semantic_projection=True,
        )
    )
    expected = sum(row[f"{table}_saved"] for row in stages.values())
    if len(rows) != expected:
        raise ComparisonError()
    grouped = defaultdict(list)
    seen = set()
    for row in rows:
        stage, payload = row["stage_id"], row["payload"]
        identity = (stage, row["ordinal"])
        if (
            stage not in stages
            or identity in seen
            or row["ordinal"] < 0
            or row["ordinal"] >= stages[stage][f"{table}_saved"]
        ):
            raise ComparisonError()
        seen.add(identity)
        key = (
            stage,
            payload.get("instrument_id"),
            payload.get("feature_id")
            if table == "features"
            else payload.get("condition_id")
            if table == "predicates"
            else None,
        )
        grouped[key].append(
            {
                "ordinal": row["ordinal"],
                "record": payload,
                "semantic": row["_semantic"],
            }
        )
    return grouped


def _targets(stages):
    grouped = defaultdict(list)
    for stage, body in stages.items():
        for kind in ("raw_targets", "constrained_plan"):
            values = body[kind]
            if values is None:
                continue
            for ordinal, value in enumerate(values):
                key = (stage, value.get("instrument_id", value.get("listing_id")), kind)
                grouped[key].append(
                    {
                        "ordinal": ordinal,
                        "target_kind": kind,
                        "record": value,
                        "semantic": value,
                    }
                )
            if not values:
                # An explicitly saved empty target list is an observation, but
                # never rewritten as a liquidation or zero target.
                grouped[(stage, None, kind)].append(
                    {
                        "ordinal": None,
                        "target_kind": kind,
                        "record": {
                            "targets": [],
                            "semantics": "observed_empty_list_not_inferred_liquidation",
                        },
                        "semantic": {"targets": []},
                    }
                )
    return grouped


def _section_absence(stages, key, table, fallback, capture):
    if fallback in {"unpaired", "not_collected"}:
        return fallback
    if table in {"features", "predicates"} and (
        capture is None or capture.mode != "research"
    ):
        return "not_collected"
    if capture and (
        (capture.stage_ids and key[0] not in capture.stage_ids)
        or (
            table in {"candidates", "features", "predicates"}
            and capture.instrument_ids
            and key[1] is not None
            and key[1] not in capture.instrument_ids
        )
    ):
        return "not_collected"
    stage = stages.get(key[0])
    if stage is None:
        # Seeing the callback does not mean a missing stage/detail was observed.
        # In particular, a fully filtered or dropped stage collection is not an
        # observed empty collection. Preserve missing-event evidence separately.
        return "incomplete_coverage" if stages or fallback == "observed" else fallback
    if stage["coverage"] != "complete":
        return "incomplete_coverage"
    if table == "targets" and stage[key[2]] is None:
        return "not_collected"
    return "observed_absence"


def _summary_state(left, right, section):
    economic = section in {
        "orders",
        "fills",
        "positions",
        "account",
        "turnover",
        "holding_duration",
    }

    def side(value):
        if economic:
            return (
                "not_collected",
                "No corresponding admitted economic observation has been compared yet.",
            )
        trace = value.trace_manifest
        if trace is None or trace.capture.mode == "off":
            return "not_collected", "Structured trace is absent or explicitly disabled."
        return (
            "partial",
            "Only recorded section observations are compared; complete whole-evaluation behavior is not certified.",
        )

    la, lr = side(left)
    ra, rr = side(right)
    return ComparisonSectionSummary(
        left_availability=la,
        right_availability=ra,
        left_reason=lr,
        right_reason=rr,
        definition=_DEFINITIONS[section],
        left_scope=_scope(left, economic),
        right_scope=_scope(right, economic),
    )


def compare_evaluations(
    left,
    right,
    output_dir,
    *,
    publication,
    control=None,
    limits=DEFAULT_LIMITS,
    durable_root=None,
):
    control = control or ComparisonControl()
    control.check()
    left, right = _snapshot(left, limits), _snapshot(right, limits)
    publication = ComparisonPublication.model_validate(publication)
    input_hash = comparison_input_hash(left, right, limits=limits)
    traces = [value.trace_manifest for value in (left, right) if value.trace_manifest]
    refs = [ref for trace in traces for ref in trace.artifacts]
    if (
        sum(ref.size_bytes for ref in refs) > limits.max_input_bytes
        or sum(ref.rows for ref in refs) > limits.max_input_rows
    ):
        raise ComparisonError(
            "comparison_capacity", "Comparison exceeds its fixed input byte/row budget"
        )
    check_file_budget(len(refs), limits)
    namespace, namespace_reason = _namespace(left, right)
    summaries = {
        section: _summary_state(left, right, section).model_dump()
        for section in SECTIONS
    }
    recorded = {section: [False, False] for section in SECTIONS}
    incomplete = {section: [False, False] for section in SECTIONS}
    for index, value in enumerate((left, right)):
        trace = value.trace_manifest
        if trace is None or trace.capture.mode == "off":
            continue
        capture = trace.capture
        filtered = bool(
            capture.observation_window
            or capture.stage_ids
            or capture.instrument_ids
            or trace.decisions_seen != trace.decisions_saved
            or trace.detail_rows_seen != trace.detail_rows_saved
        )
        for section in ("events", "candidates", "features", "predicates", "targets"):
            incomplete[section][index] = filtered
        if capture.mode != "research":
            incomplete["features"][index] = incomplete["predicates"][index] = True
    from .comparison_economics import EconomicGraph

    graph = [EconomicGraph(limits, control), EconomicGraph(limits, control)]
    observed_change = False

    def append(row):
        nonlocal observed_change
        section = row["section"]
        state = summaries[section]
        state["rows"] += 1
        state[row["status"]] += 1
        if row["ambiguous"] and row["status"] != "ambiguous":
            state["ambiguous"] += 1
        for index, side in enumerate(("left", "right")):
            recorded[section][index] |= row[side] is not None
            incomplete[section][index] |= row[f"{side}_state"] in {
                "incomplete_coverage",
                "not_collected",
                "unpaired",
            }
        actual_difference = row["status"] == "changed" or (
            row["status"] in {"left_only", "right_only"}
            and row["left_state"] in {"observed", "observed_absence"}
            and row["right_state"] in {"observed", "observed_absence"}
        )
        if actual_difference:
            state["observed_equality"] = "different"
            observed_change = True
        writer.append(row)

    try:
        with ExitStack() as stack, control.connection() as connection:
            scans = [
                TrustedTraceScan(value, stack, control, limits)
                for value in (left, right)
            ]
            events = [_event_index(connection, scan, control, limits) for scan in scans]
            pairs = _pair_events(
                connection, *events, namespace == "common", control, limits
            )
            indexed = [{row["decision_id"]: row for row in rows} for rows in events]
            # Fully paired traces need no absence-capability work. Do not add a
            # global stage scan to the normal paired-event comparison path.
            capabilities = [
                _observation_capabilities(rows, control, limits)
                if any(pair[index] is None for pair in pairs)
                else ({}, False)
                for index, rows in enumerate(events)
            ]
            if namespace == "common":
                for index, side in enumerate(("left", "right")):
                    if any(
                        pair[index] is None
                        and _pairable(indexed[1 - index][pair[1 - index]])
                        and _missing_state(
                            (left, right)[index],
                            indexed[1 - index][pair[1 - index]],
                            capabilities[index],
                        )
                        == "observed_absence"
                        for pair in pairs
                    ):
                        _target_collection_capabilities(
                            connection,
                            scans[index],
                            side,
                            capabilities[index],
                            control,
                            limits,
                        )
            writer = ComparisonWriter(
                output_dir,
                publication,
                control,
                connection,
                limits=limits,
                durable_root=durable_root,
            )
            for lid, rid in pairs:
                control.check()
                le, revent = indexed[0].get(lid), indexed[1].get(rid)
                pair_id = digest({"input_hash": input_hash, "left": lid, "right": rid})
                lh, lstages = _details(connection, scans[0], le)
                rh, rstages = _details(connection, scans[1], revent)
                ls = "observed" if le else _missing_state(left, revent, capabilities[0])
                rs = (
                    "observed" if revent else _missing_state(right, le, capabilities[1])
                )
                pairable = namespace == "common" and all(
                    _pairable(event) for event in (le, revent) if event
                )
                if not pairable:
                    ls = "unpaired" if not le else ls
                    rs = "unpaired" if not revent else rs
                # Pre-state is mandatory evidence on both sides, but path state
                # differences are never attributed to the highlighted rule.
                comparable_headers = []
                for header in (lh, rh):
                    comparable_headers.append(
                        {
                            k: v
                            for k, v in (header or {}).items()
                            if k
                            not in {
                                "decision_id",
                                "stage_coverage",
                                "pre_state",
                                "pre_state_missing_reason",
                            }
                        }
                    )
                header_equal = equal_value(*comparable_headers, control=control)
                event_status = (
                    "equal"
                    if le and revent and header_equal is True
                    else "ambiguous"
                    if le and revent and header_equal is None
                    else "changed"
                    if le and revent
                    else "left_only"
                    if le
                    else "right_only"
                )
                append(
                    {
                        "row_id": pair_id,
                        "section": "events",
                        "event_pair_id": pair_id,
                        "session_date": (le or revent)["session_date"],
                        "instrument_id": (le or revent)["instrument_id"],
                        "status": event_status,
                        "left_state": ls,
                        "right_state": rs,
                        "left": lh,
                        "right": rh,
                        "match_basis": "source_event" if le and revent else "none",
                        "ambiguous": False,
                        "missing_reason": None
                        if le and revent
                        else "Unmatched source event is retained; absence is distinguished from incomplete capture and incompatible namespaces.",
                    }
                )
                for index, (event, stages) in enumerate(
                    ((le, lstages), (revent, rstages))
                ):
                    if event:
                        from .comparison_economics import register_links

                        register_links(
                            graph[index],
                            event,
                            stages,
                            pair_id,
                            paired=bool(le and revent),
                        )
                for section in ("candidates", "features", "predicates", "targets"):
                    groups = [
                        _targets(stages)
                        if section == "targets"
                        else _observe_section(connection, scan, event, stages, section)
                        for scan, event, stages in (
                            (scans[0], le, lstages),
                            (scans[1], revent, rstages),
                        )
                    ]
                    if section == "targets":
                        for index, stages in enumerate((lstages, rstages)):
                            if any(
                                (stage["raw_targets"] is None)
                                != (stage["constrained_plan"] is None)
                                for stage in stages.values()
                            ):
                                incomplete[section][index] = True
                    for key in sorted(
                        groups[0].keys() | groups[1].keys(),
                        key=lambda key: canonical_bytes(key),
                    ):
                        lstate = _section_absence(
                            lstages,
                            key,
                            section,
                            ls
                            if le
                            else _missing_section_state(
                                left, revent, capabilities[0], key, section, ls
                            ),
                            left.trace_manifest.capture
                            if left.trace_manifest
                            else None,
                        )
                        rstate = _section_absence(
                            rstages,
                            key,
                            section,
                            rs
                            if revent
                            else _missing_section_state(
                                right, le, capabilities[1], key, section, rs
                            ),
                            right.trace_manifest.capture
                            if right.trace_manifest
                            else None,
                        )
                        row = group_record(
                            section,
                            key,
                            groups[0].get(key, []),
                            groups[1].get(key, []),
                            left_state=lstate,
                            right_state=rstate,
                            event_pair_id=pair_id,
                            session_date=(le or revent)["session_date"],
                            stage_id=key[0],
                            instrument_id=key[1],
                            control=control,
                        )
                        append(row)
                        for index, stages in enumerate((lstages, rstages)):
                            if (
                                key[0] in stages
                                and stages[key[0]]["coverage"] != "complete"
                            ):
                                incomplete[section][index] = True
            from .comparison_economics import compare_economics

            compare_economics(
                left,
                right,
                graph,
                append,
                control,
                common_namespace=namespace == "common",
            )
            for section, state in summaries.items():
                for index, side in enumerate(("left", "right")):
                    if recorded[section][index]:
                        state[f"{side}_availability"] = (
                            "partial" if incomplete[section][index] else "available"
                        )
                        state[f"{side}_reason"] = (
                            "Some compared observations have incomplete coverage or ambiguous absence; only explicitly recorded values are shown."
                            if incomplete[section][index]
                            else None
                        )
                if (
                    state["observed_equality"] != "different"
                    and state["rows"]
                    and state["equal"] == state["rows"]
                    and not any(incomplete[section])
                ):
                    state["observed_equality"] = "equal"
            equal_sections = [
                section
                for section, value in summaries.items()
                if value["observed_equality"] == "equal"
            ]
            has_behavior = any(
                summaries[section]["rows"]
                for section in ("candidates", "targets", "orders", "fills", "positions")
            )
            uncertain_rows = any(
                value["rows"] and value["observed_equality"] != "equal"
                for value in summaries.values()
            ) or any(
                value.trace_manifest is None
                or value.trace_manifest.capture.mode == "off"
                for value in (left, right)
            )
            behavior = (
                "changed"
                if observed_change
                else "unchanged_observed"
                if has_behavior and not uncertain_rows and equal_sections
                else "insufficient_coverage"
            )
            writer.finish_blocks()
            manifest = ComparisonManifest(
                publication=publication,
                input_hash=input_hash,
                left=_pins(left),
                right=_pins(right),
                field_tolerances=FIELD_TOLERANCES,
                limits=limits,
                namespace_status=namespace,
                namespace_reason=namespace_reason,
                pairing_fields=PAIR_FIELDS,
                code_changed=left.binding.code_sha256 != right.binding.code_sha256,
                configuration_differences=_configuration(left, right),
                mechanism_changes=_mechanisms(left, right),
                sections=summaries,
                observed_behavior=behavior,
                observed_equal_sections=equal_sections,
                artifacts=writer.refs,
                warnings=[
                    "Equality means identical only within the named saved observations and declared capture/window scope; it is not whole-evaluation diagnostic parity, statistical evidence, or a causal conclusion.",
                    "Both exact pre-states are retained. Prior history can change cash, positions, open orders and strategy state; this is not a local counterfactual experiment.",
                    "Native strategy and engine share a process. Final native order/fill snapshots do not certify uncollected intermediate order transitions or an independently trusted ledger.",
                    "Missing observations, filtered captures, unsupported stages and absent optional economic tables remain unknown. No latest data, strategy execution or model generation was used.",
                ],
            )
            control.check()
            result = writer.publish_manifest(manifest)
        control.check()
        return result
    except ComparisonError:
        raise
    except (
        TraceStorageError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        duckdb.Error,
    ):
        control.check()
        raise ComparisonError() from None
