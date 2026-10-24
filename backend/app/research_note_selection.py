"""Bounded workbench URL restoration, derived only from immutable admitted pins."""

import math
import re
from datetime import date
from urllib.parse import urlencode
from uuid import UUID

from backend.app.services import DomainError

SIDES = ("left", "right")
SIDE_FIELDS = {
    "source_kind",
    "source_id",
    "strategy_version_id",
    "code_sha256",
    "spec_revision",
    "spec_sha256",
    "evaluation_id",
    "execution_attempt_id",
    "result_manifest_sha256",
    "trace_manifest_sha256",
    "trace_status",
}
TEXT_KEYS = {
    "candidate_id",
    "code_sha256",
    "evaluation_id",
    "execution_attempt_id",
    "result_manifest_sha256",
    "trace_manifest_sha256",
    "trace_status",
    "decision_id",
    "instrument_id",
    "stage_id",
    "rule_id",
    "spec_sha256",
    "record_kind",
    "record_id",
    "session_date",
    "market_start",
    "market_end",
    "eligibility",
    "reason",
    "comparison_id",
    "comparison_manifest_sha256",
    "comparison_section",
    "comparison_instrument_id",
    "event_pair_id",
    "diagnostic_run_id",
    "diagnostic_config_sha256",
    "diagnostic_report_sha256",
    "diagnostic_evaluation_id",
    "diagnostic_execution_attempt_id",
    "diagnostic_result_manifest_sha256",
    "diagnostic_trace_manifest_sha256",
    "controlled_experiment_id",
    "controlled_request_sha256",
    "controlled_protocol_sha256",
    "controlled_arm_run_id",
    "controlled_arm_evaluation_id",
    "controlled_arm_execution_attempt_id",
    "controlled_arm_result_manifest_sha256",
    "controlled_arm_trace_manifest_sha256",
    "tab",
}
NUMBER_KEYS = {
    "event_offset",
    "instrument_offset",
    "candidate_offset",
    "detail_offset",
    "execution_offset",
    "market_offset",
    "rank_min",
    "rank_max",
    "score_min",
    "score_max",
    "record_ordinal",
    "spec_revision",
    "comparison_offset",
    "comparison_event_offset",
    "mechanism_index",
    "diagnostic_event_offset",
    "controlled_arm",
}
VIEW_KEYS = {
    "tab",
    "event_offset",
    "instrument_offset",
    "candidate_offset",
    "detail_offset",
    "execution_offset",
    "market_offset",
    "market_start",
    "market_end",
    "rank_min",
    "rank_max",
    "score_min",
    "score_max",
    "eligibility",
    "reason",
    "comparison_offset",
    "comparison_event_offset",
    "diagnostic_event_offset",
}
ENUMS = {
    "tab": {"description", "replay", "comparison", "verification", "notes"},
    "trace_status": {"available", "not_collected"},
    "record_kind": {"candidate", "feature", "predicate", "order"},
    "eligibility": {"eligible", "ineligible", "unknown", "not_sampled"},
    "comparison_section": {
        "events",
        "candidates",
        "features",
        "predicates",
        "targets",
        "orders",
        "fills",
        "positions",
        "account",
        "turnover",
        "holding_duration",
    },
    "reason": {
        "outside_universe",
        "feature_unavailable",
        "eligibility_filter",
        "rank_not_selected",
        "holding_retained",
        "position_slots",
        "portfolio_constraint",
        "cash_constraint",
        "timing_not_triggered",
        "order_rejected",
        "order_unfilled",
        "partial_fill",
        "not_evaluated",
        "not_collected",
        "not_sampled",
        "selected",
        "exit",
    },
}


def validate_selection(selection):
    for key, value in selection.items():
        side = next((side for side in SIDES if key.startswith(side + "_")), None)
        base = key[len(side) + 1 :] if side else key
        known = base in SIDE_FIELDS if side else key in TEXT_KEYS | NUMBER_KEYS
        if not known or type(value) not in {str, int, float}:
            raise ValueError("Unknown or non-scalar research selection")
        numeric = base == "spec_revision" if side else key in NUMBER_KEYS
        if numeric:
            if type(value) not in {int, float} or not math.isfinite(value):
                raise ValueError("Invalid research selection number")
            if not key.startswith("score_"):
                maximum = 2**53 - 1 if key == "record_ordinal" else 1_000_000
                minimum = 1 if base == "spec_revision" or key.startswith("rank_") else 0
                if value != int(value) or not minimum <= value <= maximum:
                    raise ValueError("Research selection number is out of bounds")
            if key == "controlled_arm" and value > 2:
                raise ValueError("Control arm ordinal is out of bounds")
        elif not isinstance(value, str) or not value or len(value) > 500:
            raise ValueError("Invalid research selection text")
        elif base.endswith("sha256") and not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("Invalid immutable research hash")
        elif base in {"session_date", "market_start", "market_end"}:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError("Invalid research date")
        if (
            side
            and base == "source_kind"
            and value not in {"candidate", "strategy_version"}
        ):
            raise ValueError("Invalid comparison source kind")
        if (
            side
            and base == "trace_status"
            and value not in {"registered", "historical_absence"}
        ):
            raise ValueError("Invalid comparison trace status")
        if not side and key in ENUMS and value not in ENUMS[key]:
            raise ValueError("Invalid research selection category")
    for lo, hi in (
        ("rank_min", "rank_max"),
        ("score_min", "score_max"),
        ("market_start", "market_end"),
    ):
        if lo in selection and hi in selection and selection[lo] > selection[hi]:
            raise ValueError("Inverted research selection range")


def _source(pin, strategy_id):
    value = {"code_sha256": pin.code_sha256}
    if pin.source_kind == "candidate":
        value["candidate_id"] = str(pin.source_id)
    if pin.spec_revision is not None:
        value.update(spec_revision=pin.spec_revision, spec_sha256=pin.spec_sha256)
    return UUID(str(strategy_id)), value


def _leaf(pin, strategy_id):
    root, value = _source(pin, strategy_id)
    value.update(
        evaluation_id=str(pin.evaluation_id),
        execution_attempt_id=str(pin.execution_attempt_id),
        result_manifest_sha256=pin.result_manifest_sha256,
        trace_status="available" if pin.trace_manifest_sha256 else "not_collected",
    )
    if pin.trace_manifest_sha256:
        value["trace_manifest_sha256"] = pin.trace_manifest_sha256
    return root, value


def reference_selection(ref, snapshot):
    root = UUID(snapshot["strategy_version_id"])
    if ref.kind == "source":
        root, value = _source(ref, root)
        value["tab"] = "description"
        if ref.rule_id:
            value["rule_id"] = ref.rule_id
            if snapshot.get("rule_stage_id"):
                value["stage_id"] = snapshot["rule_stage_id"]
    elif ref.kind in {"evaluation", "decision"}:
        root, value = _leaf(ref.leaf, root)
        value["tab"] = "replay"
        for key in (
            "decision_id",
            "instrument_id",
            "stage_id",
            "session_date",
            "record_kind",
            "record_id",
            "record_ordinal",
        ):
            item = getattr(ref, key)
            if item is not None:
                value[key] = item
    elif ref.kind == "comparison":
        root, primary = _leaf(ref.left, root)
        value = {
            **primary,
            "tab": "comparison",
            "comparison_id": str(ref.comparison_id),
            "comparison_manifest_sha256": ref.manifest_sha256,
            "comparison_section": ref.section,
        }
        for side, pin, source in zip(SIDES, (ref.left, ref.right), snapshot["sources"]):
            for key in SIDE_FIELDS - {"strategy_version_id", "trace_status"}:
                item = getattr(pin, key, None)
                if item is not None:
                    value[f"{side}_{key}"] = (
                        str(item) if isinstance(item, UUID) else item
                    )
            value[f"{side}_strategy_version_id"] = source["strategy_version_id"]
            value[f"{side}_trace_status"] = (
                "registered" if pin.trace_manifest_sha256 else "historical_absence"
            )
        for key, attr in (
            ("event_pair_id", "event_pair_id"),
            ("comparison_instrument_id", "instrument_id"),
            ("mechanism_index", "mechanism_index"),
        ):
            if getattr(ref, attr) is not None:
                value[key] = getattr(ref, attr)
    elif ref.kind == "diagnostic":
        from backend.app.comparison_schemas import ComparisonSideCreate

        root, primary = _leaf(
            ComparisonSideCreate.model_validate(snapshot["original_pin"]), root
        )
        value = {
            **primary,
            "tab": "verification",
            "diagnostic_run_id": str(ref.run_id),
            "diagnostic_config_sha256": ref.config_sha256,
            "diagnostic_report_sha256": ref.report_sha256,
        }
        for key in (
            "evaluation_id",
            "execution_attempt_id",
            "result_manifest_sha256",
            "trace_manifest_sha256",
        ):
            item = getattr(ref.result, key)
            if item is not None:
                value[f"diagnostic_{key}"] = (
                    str(item) if isinstance(item, UUID) else item
                )
    else:
        from backend.app.comparison_schemas import ComparisonSideCreate

        root, primary = _leaf(
            ComparisonSideCreate.model_validate(snapshot["original_pin"]), root
        )
        value = {
            **primary,
            "tab": "verification",
            "controlled_experiment_id": str(ref.controlled_experiment_id),
            "controlled_request_sha256": ref.request_sha256,
            "controlled_protocol_sha256": ref.protocol_sha256,
        }
        if ref.selected_arm is not None:
            arm = ref.arms[ref.selected_arm]
            value.update(
                controlled_arm=arm.ordinal, controlled_arm_run_id=str(arm.run_id)
            )
            if arm.result:
                for key in (
                    "evaluation_id",
                    "execution_attempt_id",
                    "result_manifest_sha256",
                    "trace_manifest_sha256",
                ):
                    item = getattr(arm.result, key)
                    if item is not None:
                        value[f"controlled_arm_{key}"] = (
                            str(item) if isinstance(item, UUID) else item
                        )
    return root, value


def href(root, selection):
    return f"/strategies/{root}/research?{urlencode(sorted(selection.items()))}"


PRIMARY_KEYS = {
    "candidate_id",
    "code_sha256",
    "spec_revision",
    "spec_sha256",
    "rule_id",
}
LEAF_KEYS = {
    "evaluation_id",
    "execution_attempt_id",
    "result_manifest_sha256",
    "trace_manifest_sha256",
    "trace_status",
}
OBSERVATION_KEYS = {
    "decision_id",
    "instrument_id",
    "stage_id",
    "session_date",
    "record_kind",
    "record_id",
    "record_ordinal",
}
COMPARISON_KEYS = {
    key for key in TEXT_KEYS | NUMBER_KEYS if key.startswith("comparison_")
} - VIEW_KEYS
COMPARISON_KEYS |= {"event_pair_id", "mechanism_index"} | {
    f"{side}_{key}" for side in SIDES for key in SIDE_FIELDS
}
DIAGNOSTIC_KEYS = {
    key for key in TEXT_KEYS | NUMBER_KEYS if key.startswith("diagnostic_")
} - VIEW_KEYS
CONTROL_KEYS = {key for key in TEXT_KEYS | NUMBER_KEYS if key.startswith("controlled_")}


def _exact_group(selection, values, keys, *, mandatory=()):
    # An absent optional selector is not substituted with a different observation.
    return all(key in selection for key in mandatory) and all(
        selection.get(key) == values.get(key) for key in keys
    )


def restore_href(restore, refs, snapshots):
    if restore is None:
        return None
    choices = [
        (ref, *reference_selection(ref, snap))
        for ref, snap in zip(refs, snapshots, strict=True)
    ]
    selection = restore.selection
    roots = [
        (ref, values)
        for ref, root, values in choices
        if root == restore.strategy_version_id
    ]
    source_identity = PRIMARY_KEYS - {"rule_id"}
    if not any(
        _exact_group(
            selection,
            values,
            source_identity,
            mandatory=("code_sha256", "spec_revision", "spec_sha256"),
        )
        for _, values in roots
    ):
        raise DomainError(
            "note_restore_mismatch",
            "Restore requires one complete exact source/spec reference",
            422,
        )
    if selection.get("rule_id") and not any(
        _exact_group(
            selection,
            values,
            source_identity
            | {"rule_id"}
            | ({"stage_id"} if not selection.get("decision_id") else set()),
        )
        for _, values in roots
    ):
        raise DomainError(
            "note_restore_mismatch",
            "Restore rule must match the exact source/spec revision",
            422,
        )
    if selection.get("evaluation_id") and not any(
        _exact_group(
            selection,
            values,
            source_identity
            | LEAF_KEYS
            | (OBSERVATION_KEYS if selection.get("decision_id") else set()),
            mandatory=(
                "evaluation_id",
                "execution_attempt_id",
                "result_manifest_sha256",
                "trace_status",
            ),
        )
        for _, values in roots
    ):
        raise DomainError(
            "note_restore_mismatch",
            "Restore leaf and observation must match one exact source-bound reference",
            422,
        )
    if any(key in selection for key in LEAF_KEYS) and not selection.get(
        "evaluation_id"
    ):
        raise DomainError(
            "note_restore_mismatch", "Restore has incomplete physical leaf pins", 422
        )
    standalone_observation = OBSERVATION_KEYS - (
        {"stage_id"} if selection.get("rule_id") else set()
    )
    if any(key in selection for key in standalone_observation) and not selection.get(
        "decision_id"
    ):
        raise DomainError(
            "note_restore_mismatch", "Restore has incomplete observation pins", 422
        )
    for kind, keys, required in (
        (
            "comparison",
            COMPARISON_KEYS,
            ("comparison_id", "comparison_manifest_sha256", "comparison_section"),
        ),
        (
            "diagnostic",
            DIAGNOSTIC_KEYS,
            (
                "diagnostic_run_id",
                "diagnostic_config_sha256",
                "diagnostic_report_sha256",
            ),
        ),
        (
            "controlled",
            CONTROL_KEYS,
            (
                "controlled_experiment_id",
                "controlled_request_sha256",
                "controlled_protocol_sha256",
            ),
        ),
    ):
        candidates = []
        for ref, root, values in choices:
            if ref.kind != kind:
                continue
            candidates.append((root, values))
            if kind == "comparison":
                # Either original side may be the primary workbench source.
                right_root, right_primary = _leaf(
                    ref.right, values["right_strategy_version_id"]
                )
                candidates.append(
                    (
                        right_root,
                        {
                            **{
                                key: value
                                for key, value in values.items()
                                if key not in source_identity | LEAF_KEYS
                            },
                            **right_primary,
                        },
                    )
                )
        if any(key in selection for key in keys) and not any(
            root == restore.strategy_version_id
            and _exact_group(
                selection,
                values,
                keys | source_identity | LEAF_KEYS,
                mandatory=required,
            )
            for root, values in candidates
        ):
            raise DomainError(
                "note_restore_mismatch",
                "Restore target must match one complete immutable evidence reference",
                422,
            )
    if selection.get("controlled_experiment_id") and any(
        ref.kind == "controlled"
        and str(ref.controlled_experiment_id) == selection["controlled_experiment_id"]
        and any(arm.status != "succeeded" for arm in ref.arms)
        for ref in refs
    ):
        return (
            None  # The present workbench cannot restore historical failed generations.
        )
    return href(restore.strategy_version_id, selection)
