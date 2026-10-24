"""Data-only original-start reexecution checks over immutable observed evidence.

No strategy imports, callbacks, predicates, models or counterfactual executions.
Local order IDs preserve edges inside each run and never identify a cross-run
source event. A successful metric comparison cannot certify absent audit/RNG.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from packages.tradegrad_adapter.runtime_observation import (
    digest as runtime_digest,
)
from packages.tradegrad_adapter.runtime_observation import (
    validate_runtime_observation,
)

from .comparison_economics import _semantic, _table
from .comparison_semantics import equal_value, multiset_equal
from .contracts import canonical_bytes, digest
from .diagnostic_parity_contracts import (
    MAX_PARITY_BYTES,
    MAX_PARITY_EVENTS,
    DiagnosticEventPair,
    DiagnosticParityCheck,
    DiagnosticParityReport,
    ParityInput,
)

DEFINITIONS = {
    "source_identity": "Exact executed source bytes, data/universe and immutable engine dependencies; newly assigned physical IDs remain distinct.",
    "fixed_conditions": "Original full evaluation window, warmup, initialization, execution profile, images, dependencies and business parameters, without a capture-policy substitution.",
    "rng_reconstruction": "Recorded original Python/NumPy-legacy state restored at the same pre-import seam; no optimizer seed or checkpoint substitutes for engine reconstruction.",
    "randomness_scope": "Exact maintained deterministic source coverage; arbitrary entropy, default_rng and self-reseeding are not proven by captured legacy RNG.",
    "audit_coverage": "Both genuine ordered engine audit streams completed without drops, sampling, framing gaps or callback errors.",
    "submitted_intents": "All observed submitted order records and explicit parent/fill topology; internal uncaptured native intents are not inferred.",
    "ordered_status_history": "Every observed status transition in each actual order's local graph, in stream order and simulated time.",
    "fills": "Actual fills including multiplicity, exact parent-order association, simulated time, quantity, price and fees.",
    "position_trajectory": "Actual recorded timestamp/instrument position trajectory; no forward-fill or reconstructed holdings.",
    "account_trajectory": "Actual cash/equity/margin/drawdown observations, including engine audit equity events; no interpolation.",
    "result_metrics": "Economic statistics, fees, closed trades and benchmark, excluding optimizer reward and representation-only v1/v2 exporter labels.",
    "original_event_mapping": "Unique frozen data_event_id + callback_phase + scope + event_seq + instrument mapping in the requested retained observation scope; random IDs/date-only joins are forbidden.",
    "decision_pre_states": "Both actual decision-before states, retaining explicit local open-order associations and missing-state declarations.",
    "observed_targets": "Already-recorded raw targets and constrained plans for the selected shared stages; no predicate or sizing reevaluation.",
}


def _check(
    key, left=None, right=None, *, unavailable=None, sequence=False, control=None
):
    if unavailable is not None:
        return DiagnosticParityCheck(
            key=key,
            status="not_verifiable",
            definition=DEFINITIONS[key],
            reason=unavailable,
        )
    same = (
        canonical_bytes(left) == canonical_bytes(right)
        if key in {"source_identity", "fixed_conditions", "rng_reconstruction"}
        else equal_value(left, right, control=control)
        if sequence
        else multiset_equal(left, right, control=control)
    )
    status = (
        "matched" if same is True else "mismatch" if same is False else "not_verifiable"
    )
    examples = (
        []
        if same is True
        else [{"source_sha256": digest(left), "replay_sha256": digest(right)}]
    )
    return DiagnosticParityCheck(
        key=key,
        status=status,
        definition=DEFINITIONS[key],
        reason="Bounded semantic matching could not establish equality."
        if same is None
        else None,
        source_count=len(left) if isinstance(left, (list, dict)) else None,
        replay_count=len(right) if isinstance(right, (list, dict)) else None,
        mismatch_count=int(same is False),
        examples=examples,
    )


def _maintained(source):
    # Only exact executable bytes of reviewed deterministic templates qualify.
    # A comment/parameter/source mutation is never assumed to preserve coverage.
    from .templates import available_templates, template_source

    if any(source == template_source(name) for name in available_templates()):
        return True
    from packages.tradegrad_adapter.portable_prompts import _TEMPLATE_ROOT
    from packages.tradegrad_adapter.portable_prompts import (
        template_source as portable_source,
    )

    return any(
        source == portable_source(kind) for kind in ("event", "portfolio")
    ) or any(
        source == (_TEMPLATE_ROOT / name).read_text(encoding="utf-8")
        for name in ("event_trend.py", "portfolio_momentum.py")
    )


def _identity(value):
    pins = value.pins.binding
    if (
        hashlib.sha256(value.source_code.encode()).hexdigest() != pins.code_sha256
        or digest(value.bundle) != pins.result_manifest_sha256
    ):
        raise ValueError("Parity source bytes differ from immutable pins")
    observation = value.runtime_observation
    if observation is None:
        if value.pins.runtime_observation_sha256 is not None:
            raise ValueError("Pinned runtime observation is absent")
    else:
        validate_runtime_observation(
            dict(observation), expected_identity=value.bundle["identity"]
        )
        if runtime_digest(observation) != value.pins.runtime_observation_sha256:
            raise ValueError("Parity runtime observation differs from immutable pin")
    return {
        key: (
            str(getattr(pins, key))
            if key.endswith("_id") and getattr(pins, key) is not None
            else getattr(pins, key)
        )
        for key in (
            "code_sha256",
            "dataset_version_id",
            "dataset_sha256",
            "universe_version_id",
            "universe_sha256",
            "engine_dependencies_sha256",
        )
    }


def _audit(value):
    observation = value.runtime_observation
    if observation is None or not isinstance(observation.get("audit"), dict):
        return None
    audit = observation["audit"]
    if not audit["completed"] or audit["loss_reasons"]:
        return None
    return audit


def _timestamp_ns(value):
    """Exact ISO8601 conversion without float epoch arithmetic or lost ns."""
    if not isinstance(value, str):
        raise TypeError("Missing observed timestamp")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("Unzoned observed timestamp")
    elapsed = moment.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    fraction = re.search(r"T\d{2}:\d{2}:\d{2}\.(\d+)", value)
    ns = int((fraction.group(1) + "000000000")[:9]) if fraction else 0
    return (elapsed.days * 86400 + elapsed.seconds) * 1_000_000_000 + ns


def _account_audit_complete(value, audit):
    if audit is None:
        return False
    last = {}
    try:
        for event in audit["events"]:
            if event["event_type"] == "equity":
                if int(event["payload"]["timestamp"]) != event["ts"]:
                    return False
                last[event["ts"]] = event["payload"]
        portable = value.bundle.get("evaluation_mode") == "portable"
        series = (
            value.bundle.get("account_series")
            if portable
            else _table(value.bundle, "equity")
        )
        if not isinstance(series, list) or not series or not last:
            return False
        for row in series:
            stamp = (
                int(row["native_timestamp_ns"])
                if portable
                else _timestamp_ns(row["timestamp"])
            )
            if stamp not in last or not equal_value(
                float(last[stamp]["equity"]), row["equity"], "equity"
            ):
                return False
            if portable and not equal_value(
                float(last[stamp]["cash"]), row["cash"], "cash"
            ):
                return False
        if not portable:
            for field in ("cash", "margin"):
                for row in _table(value.bundle, field) or []:
                    stamp = _timestamp_ns(row["timestamp"])
                    if stamp not in last or not equal_value(
                        float(last[stamp][field]), row[field], field
                    ):
                        return False
    except (ValueError, TypeError, KeyError):
        return False
    return True


def _graphs(value):
    """Schema-specific local order-ID resolution, never generic ID removal."""
    portable = value.bundle.get("evaluation_mode") == "portable"
    orders, fills = _table(value.bundle, "orders"), _table(value.bundle, "fills")
    if orders is None or fills is None:
        return None
    order_field, fill_field = ("order_id", "fill_id") if portable else ("id", "id")
    indexed = {}
    for row in orders:
        key = row.get(order_field)
        if not isinstance(key, str) or key in indexed:
            raise ValueError("Invalid local order identity")
        indexed[key] = {
            "order": _semantic(row, "orders", portable),
            "statuses": [],
            "fills": [],
            "audit_fills": [],
        }
    fill_ids = set()
    for row in fills:
        identifier, parent = row.get(fill_field), row.get("order_id")
        if (
            not isinstance(identifier, str)
            or identifier in fill_ids
            or parent not in indexed
        ):
            raise ValueError("Invalid explicit fill edge")
        fill_ids.add(identifier)
        indexed[parent]["fills"].append(_semantic(row, "fills", portable))
    if portable:
        original_orders = {row[order_field]: row for row in orders}
        logical = value.bundle.get("logical_orders")
        if not isinstance(logical, list):
            return None
        assigned, logical_ids = set(), set()
        for item in logical:
            identifier = item.get("logical_order_id")
            children = item.get("native_order_ids")
            if (
                not isinstance(identifier, str)
                or identifier in logical_ids
                or not isinstance(children, list)
                or not children
                or len(children) != len(set(children))
                or any(child not in indexed or child in assigned for child in children)
            ):
                return None
            logical_ids.add(identifier)
            group = {
                "logical": {
                    k: v
                    for k, v in item.items()
                    if k not in {"logical_order_id", "native_order_ids"}
                },
                "children": sorted(
                    [indexed[child]["order"] for child in children], key=canonical_bytes
                ),
            }
            for child in children:
                if original_orders[child].get("logical_order_id") != identifier:
                    return None
                indexed[child]["logical_group"] = group
                assigned.add(child)
        if assigned != set(indexed) or any(
            row.get("logical_order_id")
            != original_orders[row["order_id"]].get("logical_order_id")
            for row in fills
        ):
            return None
    audit = _audit(value)
    # Portable engine receipts keep native IDs; the normalized result must expose
    # their explicit native_id association. No UUID-order/row-order approximation.
    aliases = {}
    for row in orders:
        aliases[row[order_field]] = row[order_field]
        native = row.get("native_order_id")
        if isinstance(native, str):
            if native in aliases and aliases[native] != row[order_field]:
                raise ValueError("Ambiguous portable native order edge")
            aliases[native] = row[order_field]
    if audit is not None:
        for event in audit["events"]:
            kind = event["event_type"]
            if kind not in {"order", "trade"}:
                continue
            payload = event["payload"]
            parent = aliases.get(payload.get("order_id"))
            if parent is None:
                return None  # unsupported explicit engine/result edge, never guess
            omitted = {"order_id"} if kind == "order" else {"order_id", "trade_id"}
            semantic = {
                "ts": event["ts"],
                "symbol": event.get("symbol"),
                "payload": {k: v for k, v in payload.items() if k not in omitted},
            }
            indexed[parent]["statuses" if kind == "order" else "audit_fills"].append(
                semantic
            )
    if audit is not None:
        seen_trades = set()
        fill_index = {row[fill_field]: row for row in fills}
        for event in audit["events"]:
            if event["event_type"] != "trade":
                continue
            payload = event["payload"]
            identifier = payload.get("trade_id")
            if identifier not in fill_index or identifier in seen_trades:
                return None
            fill = fill_index[identifier]
            if aliases.get(payload.get("order_id")) != fill.get("order_id"):
                return None
            if (
                str(payload.get("side", "")).lower() != fill["side"]
                or event.get("symbol") != fill["listing_id" if portable else "symbol"]
            ):
                return None
            try:
                if not equal_value(
                    float(payload["quantity"]), fill["quantity"], "quantity"
                ) or not equal_value(float(payload["price"]), fill["price"], "price"):
                    return None
            except (ValueError, TypeError, KeyError):
                return None
            if event["ts"] != (
                int(fill["native_timestamp_ns"])
                if portable
                else _timestamp_ns(fill["timestamp"])
            ):
                return None
            seen_trades.add(identifier)
        if seen_trades != fill_ids or any(
            not item["statuses"] for item in indexed.values()
        ):
            return None
        for item in indexed.values():
            observed = item["statuses"][-1]["payload"].get("status", "")
            final = item["order"].get("status", "")
            normalized = (
                {
                    "New": "accepted",
                    "Submitted": "accepted",
                    "Accepted": "accepted",
                    "PartiallyFilled": "partial_fill",
                    "Filled": "filled",
                    "Canceled": "cancelled",
                    "Cancelled": "cancelled",
                    "Rejected": "rejected",
                    "Expired": "expired",
                }.get(observed)
                if portable
                else observed.lower().replace("_", "")
            )
            if normalized != (final if portable else final.lower().replace("_", "")):
                return None
    # Compute once per local graph, not once per referencing decision. This
    # exact identity anchor is deliberately stricter than economic tolerances.
    for item in indexed.values():
        item["observed_graph_sha256"] = digest(item)
    return indexed


def _graph_values(graph, kind):
    values = []
    for item in graph.values():
        projected = {"order": item["order"], "logical_group": item.get("logical_group")}
        if kind == "ordered_status_history":
            projected["ordered_statuses"] = item["statuses"]
        else:
            projected["fills"] = item["fills"]
            if kind == "fills":
                projected["audit_fills"] = item["audit_fills"]
        values.append(projected)
    return values


def _prestate(record, graph):
    state = record.get("pre_state")
    if state is None or graph is None:
        return None
    if state.get("coverage") != "complete":
        warmup_reason = "Warmup is state-only replay before native broker startup; no native account, positions or open orders were observed."
        if (
            record.get("callback_phase") == "portable.complete_daily_cross_section"
            and state.get("coverage") == "partial"
            and state.get("source") == "adapter_observed"
            and state.get("missing_reason") == warmup_reason
            and state.get("strategy_state") is not None
            and all(
                state.get(k) is None
                for k in ("cash", "equity", "positions", "open_orders")
            )
        ):
            return {
                "basis": "observed_policy_state_before_native_broker_startup",
                **state,
            }
        return None
    result = {
        key: value
        for key, value in state.items()
        if key not in {"source", "open_orders"}
    }
    orders = []
    for row in state["open_orders"]:
        if "native_order_ids" in row:
            children = row["native_order_ids"]
            if (
                not isinstance(children, list)
                or not children
                or len(children) != len(set(children))
                or any(child not in graph for child in children)
            ):
                return None
            orders.append(
                {
                    **{
                        k: v
                        for k, v in row.items()
                        if k not in {"order_id", "native_order_ids"}
                    },
                    "explicit_native_children": sorted(
                        [
                            {
                                "order": graph[child]["order"],
                                "observed_graph_sha256": graph[child].get(
                                    "observed_graph_sha256"
                                )
                                or digest(graph[child]),
                            }
                            for child in children
                        ],
                        key=canonical_bytes,
                    ),
                }
            )
        else:
            local = row.get("order_id")
            if local not in graph:
                return None
            # Only the documented open_orders.order_id is a local execution key.
            orders.append(
                {
                    **{k: v for k, v in row.items() if k != "order_id"},
                    "explicit_order_semantics": graph[local]["order"],
                    # A final order row is not a unique local graph: partial
                    # fills/status paths can differ with the same totals. Pin
                    # the ID-free observed child graph, not a guessed row match.
                    "observed_graph_sha256": graph[local].get("observed_graph_sha256")
                    or digest(graph[local]),
                }
            )
    result["open_orders"] = sorted(orders, key=canonical_bytes)
    result["positions"] = sorted(result["positions"], key=canonical_bytes)
    return result


def _event_key(row):
    return (
        row["data_event_id"],
        row["callback_phase"],
        row["scope"],
        row["event_seq"],
        row.get("instrument_id"),
    )


def _selected(row, capture):
    if row["data_event_id"].startswith("native_order:") or row[
        "callback_phase"
    ].startswith("native.execution_result:"):
        return False
    window = (capture or {}).get("observation_window")
    if window:
        day = (
            datetime.fromisoformat(row["decision_time"])
            .astimezone(ZoneInfo(row["market_timezone"]))
            .date()
            .isoformat()
        )
        if not window["start"] <= day < window["end"]:
            return False
    return True


def _portable_targets(row, capture):
    from .portable import INTERNALS_MISSING

    scope_gap = "Detailed observations outside the configured capture mode/scope were not sampled; the full candidate pool still determined the decision."
    receipt_gap = "Submission receipts expose actual native child IDs, not final status, creation time or fills. Those are observed separately from the native execution result."
    stages = {stage["stage_id"]: stage for stage in row.get("stages", [])}
    selected = set((capture or {}).get("stage_ids") or stages)
    if not selected.issubset(stages) or "portable.policy_reply" not in selected:
        return None
    reply = stages["portable.policy_reply"]
    if reply.get("coverage") != "partial" or reply.get("missing_reason") not in {
        INTERNALS_MISSING,
        scope_gap,
    }:
        return None
    event = "portable.order_intents" in stages
    required = "portable.order_intents" if event else "portable.controller_plan"
    if required not in selected:
        state = row.get("pre_state") or {}
        if (
            not event
            and required not in stages
            and reply.get("raw_targets") == []
            and state.get("coverage") == "partial"
            and state.get("source") == "adapter_observed"
            and state.get("missing_reason")
            == "Warmup is state-only replay before native broker startup; no native account, positions or open orders were observed."
        ):
            return {
                "channel": "state_only_warmup",
                "raw_targets": [],
                "controller_plan": "not_applicable_before_native_startup",
            }
        return None
    plan = stages[required]
    if plan.get("coverage") != "complete" and not (
        plan.get("coverage") == "partial"
        and plan.get("missing_reason") in {receipt_gap, scope_gap}
    ):
        return None
    if not isinstance(plan.get("constrained_plan"), list):
        return None
    if event:
        if reply.get("raw_targets") is not None:
            return None
        result = {
            "channel": "event_intents",
            "received_intents": plan["constrained_plan"],
        }
    else:
        if not isinstance(reply.get("raw_targets"), list):
            return None
        result = {
            "channel": "portfolio_targets",
            "raw_targets": reply["raw_targets"],
            "constrained_plan": plan["constrained_plan"],
        }
    instruments = set((capture or {}).get("instrument_ids") or [])
    return {
        key: [
            item
            for item in value
            if not instruments or item.get("listing_id") in instruments
        ]
        if isinstance(value, list)
        else value
        for key, value in result.items()
    }


def _targets(row, capture):
    if row.get("callback_phase") == "portable.complete_daily_cross_section":
        return _portable_targets(row, capture)
    selected = (capture or {}).get("stage_ids")
    instruments = set((capture or {}).get("instrument_ids") or [])
    stages = row.get("stages", [])
    available = {stage["stage_id"] for stage in stages}
    if selected and not set(selected).issubset(available):
        return None
    result = {}
    non_target_stages = {
        "features",
        "universe",
        "liquidity",
        "ranking",
        "signal",
        "execution",
    }
    scope_gap = "Detailed observations outside the configured capture mode/scope were not sampled; the full candidate pool still determined the decision."
    for stage in stages:
        if selected and stage["stage_id"] not in selected:
            continue
        raw, plan = stage.get("raw_targets"), stage.get("constrained_plan")
        if raw is None and plan is None and stage["stage_id"] in non_target_stages:
            continue  # explicitly schema-known non-target stage, not an empty plan
        if (
            raw is None
            or plan is None
            or stage.get("coverage") != "complete"
            and not (
                stage.get("coverage") == "partial"
                and stage.get("missing_reason") == scope_gap
            )
        ):
            return None
        result[stage["stage_id"]] = {
            key: [
                r
                for r in rows
                if not instruments or r.get("instrument_id") in instruments
            ]
            for key, rows in (("raw_targets", raw), ("constrained_plan", plan))
        }
    return result or None  # no target stage is unknown, never an observed [] plan


def _pair_events(source, replay, graphs, control):
    left, right = defaultdict(list), defaultdict(list)
    for value, target in ((source, left), (replay, right)):
        for row in value.decisions:
            if _selected(row, replay.capture):
                target[_event_key(row)].append(row)
    if len(left.keys() | right.keys()) > MAX_PARITY_EVENTS:
        reason = "Combined event map exceeds the explicit 10,000-event diagnostic capacity; no event mapping was sampled."
        return (
            [],
            [
                _check(key, unavailable=reason)
                for key in (
                    "original_event_mapping",
                    "decision_pre_states",
                    "observed_targets",
                )
            ],
            sum(map(len, left.values())),
            sum(map(len, right.values())),
        )
    pairs, pre_left, pre_right, target_left, target_right = [], [], [], [], []
    mapping_bad, pre_missing, target_missing = 0, 0, 0
    metadata_mismatches = []
    for key in sorted(left.keys() | right.keys(), key=canonical_bytes):
        if control:
            control.check()
        a, b = left.get(key, []), right.get(key, [])
        ambiguous = len(a) > 1 or len(b) > 1
        lr, rr = a[0] if len(a) == 1 else None, b[0] if len(b) == 1 else None
        states = (
            _prestate(lr, graphs[0]) if lr else None,
            _prestate(rr, graphs[1]) if rr else None,
        )
        same_state = (
            equal_value(*states, control=control)
            if all(x is not None for x in states)
            else None
        )
        targets = (
            _targets(lr, replay.capture) if lr else None,
            _targets(rr, replay.capture) if rr else None,
        )
        same_target = (
            equal_value(*targets, control=control)
            if all(x is not None for x in targets)
            else None
        )
        mapping_bad += bool(ambiguous or lr is None or rr is None)
        pre_missing += same_state is None
        target_missing += same_target is None
        if lr and rr:
            metadata_fields = ("decision_time", "information_cutoff", "market_timezone")
            if any(lr.get(field) != rr.get(field) for field in metadata_fields):
                metadata_mismatches.append(
                    {
                        "pairing_key": key,
                        "original_decision_id": lr["decision_id"],
                        "replay_decision_id": rr["decision_id"],
                        "reason": "Recorded event timing/information cutoff metadata differs.",
                    }
                )
            pre_left.append({"event": key, "state": states[0]})
            pre_right.append({"event": key, "state": states[1]})
            target_left.append({"event": key, "targets": targets[0]})
            target_right.append({"event": key, "targets": targets[1]})
        pairs.append(
            DiagnosticEventPair(
                pairing_key=key,
                original_decision_id=lr["decision_id"] if lr else None,
                replay_decision_id=rr["decision_id"] if rr else None,
                original_pre_state_sha256=digest(lr["pre_state"])
                if lr and lr.get("pre_state") is not None
                else None,
                replay_pre_state_sha256=digest(rr["pre_state"])
                if rr and rr.get("pre_state") is not None
                else None,
                pre_state_status="matched"
                if same_state is True
                else "mismatch"
                if same_state is False
                else "not_verifiable",
                observation_status="matched"
                if same_target is True
                else "mismatch"
                if same_target is False
                else "not_verifiable",
                mapping="ambiguous"
                if ambiguous
                else "unpaired"
                if lr is None or rr is None
                else "unverified_observed_pairing",
                reason="Duplicate or unmatched frozen source event."
                if ambiguous or lr is None or rr is None
                else None,
            )
        )
    if {row["decision_id"] for rows in left.values() for row in rows} & {
        row["decision_id"] for rows in right.values() for row in rows
    }:
        raise ValueError("Diagnostic decision IDs must not reuse original IDs")
    missing = source.capture_missing + replay.capture_missing
    reason = (
        "; ".join(missing)
        if missing
        else "No comparable original structured events were retained."
        if not pairs
        else None
    )
    mapping = _check(
        "original_event_mapping",
        list(left),
        list(right),
        sequence=False,
        unavailable=reason,
        control=control,
    )
    if metadata_mismatches:
        mapping = mapping.model_copy(
            update={
                "status": "mismatch",
                "reason": "Recorded source-event metadata differs.",
                "mismatch_count": len(metadata_mismatches),
                "examples": metadata_mismatches[:20],
            }
        )
    if mapping_bad and mapping.status == "matched":
        mapping = mapping.model_copy(
            update={
                "status": "not_verifiable",
                "reason": "Duplicate frozen event keys cannot establish individual identity.",
            }
        )
    pre = _check(
        "decision_pre_states",
        pre_left,
        pre_right,
        sequence=True,
        unavailable=reason
        or (
            "At least one decision-before state is missing or cannot resolve its local orders."
            if pre_missing
            else None
        ),
        control=control,
    )
    targets = _check(
        "observed_targets",
        target_left,
        target_right,
        sequence=True,
        unavailable=reason
        or (
            "At least one event has no paired retained targets/plans."
            if target_missing
            else None
        ),
        control=control,
    )
    return (
        pairs,
        [mapping, pre, targets],
        sum(map(len, left.values())),
        sum(map(len, right.values())),
    )


def compare_diagnostic(source: ParityInput, replay: ParityInput, *, control=None):
    checks = [
        _check(
            "source_identity",
            _identity(source),
            _identity(replay),
            sequence=True,
            control=control,
        ),
        _check(
            "fixed_conditions",
            source.fixed_conditions,
            replay.fixed_conditions,
            sequence=True,
            control=control,
        ),
    ]
    if (
        source.pins.binding.evaluation_id == replay.pins.binding.evaluation_id
        or source.pins.binding.execution_attempt_id
        == replay.pins.binding.execution_attempt_id
    ):
        raise ValueError("Diagnostic replay must have a new physical leaf")
    observations = source.runtime_observation, replay.runtime_observation
    if all(o is not None for o in observations):
        a, b = observations
        checks.append(
            _check(
                "rng_reconstruction",
                {
                    "initialization": a["initialization"],
                    "state": a["rng"]["state"],
                    "restored": a["rng"]["state_sha256"],
                },
                {
                    "initialization": b["initialization"],
                    "state": b["rng"]["state"],
                    "restored": b["rng"]["restored_from_sha256"],
                },
                sequence=True,
                control=control,
            )
        )
    else:
        checks.append(
            _check(
                "rng_reconstruction",
                unavailable="Original or replay runtime/RNG initialization evidence was not recorded.",
            )
        )
    checks.append(
        _check(
            "randomness_scope",
            [],
            [],
            unavailable=None
            if _maintained(source.source_code) and _maintained(replay.source_code)
            else "This source is not an exact reviewed deterministic template; unrecorded generators and external entropy cannot be certified.",
        )
    )
    audits = _audit(source), _audit(replay)
    audit_missing = (
        "Original or replay ordered audit is absent, incomplete, sampled or dropped."
        if any(a is None for a in audits)
        else None
    )
    if audit_missing is None and not all(
        _account_audit_complete(value, audit)
        for value, audit in zip((source, replay), audits, strict=True)
    ):
        audit_missing = "Engine audit equity observations do not cover the actual account trajectory."
    checks.append(_check("audit_coverage", [], [], unavailable=audit_missing))
    graphs = _graphs(source), _graphs(replay)
    graph_missing = (
        "Observed order/fill identities cannot be resolved through explicit local engine/result edges."
        if any(g is None for g in graphs)
        else None
    )
    for key in ("submitted_intents", "ordered_status_history", "fills"):
        checks.append(
            _check(
                key,
                *(
                    [_graph_values(g, key) for g in graphs]
                    if not graph_missing
                    else [None, None]
                ),
                unavailable=graph_missing
                or (audit_missing if key != "submitted_intents" else None),
                control=control,
            )
        )
    positions = [_table(v.bundle, "positions") for v in (source, replay)]
    checks.append(
        _check(
            "position_trajectory",
            *positions,
            unavailable="Position observations are missing."
            if any(v is None for v in positions)
            else None,
            control=control,
        )
    )
    accounts = []
    for value, audit in zip((source, replay), audits, strict=True):
        portable = value.bundle.get("evaluation_mode") == "portable"
        accounts.append(
            {
                "series": value.bundle.get("account_series")
                if portable
                else {
                    key: _table(value.bundle, key)
                    for key in ("cash", "equity", "margin", "drawdown")
                },
                "audit_equity": [
                    {"ts": e["ts"], "symbol": e.get("symbol"), "payload": e["payload"]}
                    for e in audit["events"]
                    if e["event_type"] == "equity"
                ]
                if audit
                else None,
            }
        )
    checks.append(
        _check(
            "account_trajectory",
            *accounts,
            sequence=True,
            unavailable=audit_missing,
            control=control,
        )
    )
    metrics = [
        {
            "native_metrics": v.bundle.get("native_metrics"),
            "fees": v.bundle.get("fees"),
            "closed_trades": _table(v.bundle, "closed_trades"),
            "benchmark": v.bundle.get("benchmark"),
            "portable_metrics": v.bundle.get("summary")
            if v.bundle.get("evaluation_mode") == "portable"
            else None,
        }
        for v in (source, replay)
    ]
    checks.append(_check("result_metrics", *metrics, sequence=True, control=control))
    pairs, event_checks, left_count, right_count = _pair_events(
        source, replay, graphs, control
    )
    checks.extend(event_checks)
    outcome = (
        "mismatch"
        if any(c.status == "mismatch" for c in checks)
        else "not_verifiable"
        if any(c.status == "not_verifiable" for c in checks)
        else "matched"
    )
    if outcome == "matched":
        pairs = [
            p.model_copy(update={"mapping": "verified_original_event"}) for p in pairs
        ]
    if len(pairs) > MAX_PARITY_EVENTS:
        raise ValueError("Parity event map exceeds its declared bound")
    report = DiagnosticParityReport(
        source=source.pins,
        replay=replay.pins,
        outcome=outcome,
        checks=checks,
        events=pairs,
        original_event_count=left_count,
        replay_event_count=right_count,
        observation_scope={
            "source_capture": source.capture,
            "replay_capture": replay.capture,
            "event_pairing_scope": "retained_original_events_within_replay_observation_window",
            "execution_scope": "full_original_evaluation_and_warmup",
            "excluded_from_decision_pairing": "generic native_order IDs and derived native.execution_result records; actual order/status/fill graphs checked separately",
        },
        limitations=[
            "Native strategy and engine share one process; this is not trusted-ledger, absence-of-lookahead or causal proof.",
            "Only named numerical fields use the fixed published tolerances; IDs, timestamps, discrete quantities and missing states compare exactly.",
            "Internal native intents not emitted by the engine remain unobserved; submitted orders are the explicit observable intent boundary.",
            "New diagnostic decision IDs are retained. Original-ID mapping is verified only when every required named parity check matches.",
        ],
    )
    if len(canonical_bytes(report)) > MAX_PARITY_BYTES:
        raise ValueError("Parity report exceeds its declared byte bound")
    return report
