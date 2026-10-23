"""Opt-in copying observer. It never evaluates a rule or changes its inputs.

Collection failures and explicit budgets produce partial coverage, not invented
values. This is strategy-emitted evidence in the native shared process, not an
independently trusted ledger or no-lookahead proof.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from .contracts import CaptureConfig, DecisionRecord, StageObservation, canonical_bytes

_ACTIVE: ContextVar[Recorder | None] = ContextVar("research_trace", default=None)


def current_recorder():
    return _ACTIVE.get()


@contextmanager
def recording(recorder):
    token = _ACTIVE.set(recorder)
    try:
        yield recorder
    finally:
        if recorder is not None:
            recorder.end_decision()
        _ACTIVE.reset(token)


class CaptureLimit(ValueError):
    """Declared observation capacity was reached before an expensive copy."""


def _copy(value, depth=0, budget=None):
    """Reject executable objects and bound traversal before allocating a copy."""
    if budget is None:
        budget = [1_048_576]
    if depth > 24:
        raise ValueError("Trace observation nesting exceeds limit")
    budget[0] -= 2
    if budget[0] < 0:
        raise CaptureLimit()
    if value is None or type(value) in (int, float, bool):
        budget[0] -= len(canonical_bytes(value))
        if budget[0] < 0:
            raise CaptureLimit()
        return value
    if type(value) is str:
        if len(value) > budget[0]:
            raise CaptureLimit()
        budget[0] -= len(canonical_bytes(value))
        if budget[0] < 0:
            raise CaptureLimit()
        return value
    if type(value) is datetime:
        if type(value.tzinfo) not in (timezone, ZoneInfo):
            raise ValueError(
                "Only standard timezone-aware datetime values are accepted"
            )
        return _copy(value.isoformat(), depth + 1, budget)
    if type(value) is list or type(value) is tuple:
        if len(value) > budget[0] // 2:
            raise CaptureLimit()
        return [_copy(item, depth + 1, budget) for item in value]
    if type(value) is dict:
        if len(value) > budget[0] // 4 or any(type(key) is not str for key in value):
            raise CaptureLimit()
        return {
            _copy(key, depth + 1, budget): _copy(item, depth + 1, budget)
            for key, item in value.items()
        }
    raise ValueError("Trace accepts already-computed plain JSON values only")


class Recorder:
    def __init__(
        self,
        *,
        code_sha256: str,
        config: CaptureConfig | None = None,
        on_record: Callable[[DecisionRecord], None] | None = None,
    ):
        self.code_sha256 = code_sha256
        self.config = config or CaptureConfig()
        self._on_record = on_record
        self.records: list[DecisionRecord] = []
        self._current = None
        self._selected = False
        self._pending_rows = self._pending_bytes = 0
        self.decisions_seen = self.decisions_saved = 0
        self.detail_rows_seen = self.detail_rows_saved = 0
        self.saved_bytes = 0
        self.missing_reasons: set[str] = set()
        self.stage_coverage: dict[str, str] = {}
        self._event_keys: set[tuple] = set()

    def _failed(self, reason):
        # Never include arbitrary exception text, source values or paths.
        if len(self.missing_reasons) < 99:
            self.missing_reasons.add(reason)
        else:
            self.missing_reasons.add(
                "Additional collection gaps exceeded the bounded reason summary; saved stage reasons remain available."
            )

    def begin_decision(
        self,
        *,
        data_event_id,
        event_seq,
        scope,
        callback_phase,
        decision_time,
        information_cutoff,
        market_timezone,
        instrument_id=None,
        pre_state=None,
        pre_state_missing_reason=None,
    ):
        self.end_decision()
        self._pending_rows = self._pending_bytes = 0
        self.decisions_seen += 1
        decision_id = str(uuid4())
        if self.config.mode == "off":
            self._failed("Capture was explicitly disabled.")
            return decision_id
        try:
            self._current = _copy(
                {
                    "decision_id": decision_id,
                    "data_event_id": data_event_id,
                    "event_seq": event_seq,
                    "scope": scope,
                    "callback_phase": callback_phase,
                    "decision_time": decision_time,
                    "information_cutoff": information_cutoff,
                    "market_timezone": market_timezone,
                    "instrument_id": instrument_id,
                    "pre_state": pre_state,
                    "pre_state_missing_reason": pre_state_missing_reason
                    if pre_state_missing_reason is not None
                    else (
                        "The adapter did not capture decision-before portfolio state."
                        if pre_state is None
                        else None
                    ),
                    "stages": [],
                },
                budget=[self.config.max_uncompressed_bytes - self.saved_bytes],
            )
            moment = datetime.fromisoformat(self._current["decision_time"])
            window = self.config.observation_window
            self._selected = (
                window is None
                or window.start
                <= moment.astimezone(ZoneInfo(market_timezone)).date()
                < window.end
            )
            if not self._selected:
                self._failed(
                    "Decisions outside the requested observation window were not sampled; computation was unchanged."
                )
            if self.decisions_saved >= self.config.max_decisions:
                self._selected = False
                self._failed("Decision collection reached its declared capacity limit.")
        except CaptureLimit:
            self._current = None
            self._selected = False
            self._failed(
                "Decision collection reached its declared byte capacity limit before copying."
            )
        except Exception:  # noqa: BLE001 - an observer must not alter strategy execution
            self._current = None
            self._selected = False
            self._failed(
                "Decision collection failed validation; no replacement value was fabricated."
            )
        return decision_id

    def emit(self, stage_id, observation):
        if type(stage_id) is not str or not 1 <= len(stage_id) <= 200:
            self._failed("Invalid stage identity was not converted or evaluated.")
            return
        if stage_id not in self.stage_coverage and len(self.stage_coverage) >= 100:
            self._failed(
                "Distinct stage metadata reached its declared capacity of 100."
            )
            return
        if self.config.mode == "off" or self._current is None:
            return
        try:
            if type(observation) is not dict or any(
                type(key) is not str for key in observation
            ):
                raise ValueError("Stage observation must be a plain dictionary")
            if any(
                type(observation.get(key, [])) is not list
                for key in ("features", "predicates", "candidates")
            ):
                raise ValueError("Observation rows must be plain lists")
            count = sum(
                len(observation.get(key, []))
                for key in ("features", "predicates", "candidates")
            )
            self.detail_rows_seen += count
            if not self._selected:
                return
            if self.config.stage_ids and stage_id not in self.config.stage_ids:
                self.stage_coverage[stage_id] = "not_sampled"
                self._failed(
                    "Stages outside the requested capture scope were not sampled."
                )
                return
            item = _copy(
                observation,
                budget=[
                    self.config.max_uncompressed_bytes
                    - self.saved_bytes
                    - self._pending_bytes
                ],
            )
            item.setdefault("stage_id", stage_id)
            if item["stage_id"] != stage_id:
                raise ValueError("Stage identity differs")
            item.setdefault("coverage", "complete")
            item.setdefault("missing_reason", None)
            for key in ("features", "predicates", "candidates", "order_links"):
                for row in item.get(key, []):
                    row.setdefault("source", "strategy_emitted")
                    ref = row.get("source_ref")
                    if ref is not None:
                        ref.setdefault("code_sha256", self.code_sha256)
                        if ref["code_sha256"] != self.code_sha256:
                            raise ValueError("Source belongs to another version")
            sampled = False
            if self.config.mode == "summary":
                features = item.pop("features", [])
                predicates = item.pop("predicates", [])
                sampled = bool(features or predicates)
            if self.config.instrument_ids:
                permitted = set(self.config.instrument_ids)
                for key in ("features", "predicates", "candidates"):
                    before = item.get(key, [])
                    item[key] = [
                        row
                        for row in before
                        if row.get("instrument_id") is None
                        or row["instrument_id"] in permitted
                    ]
                    sampled = sampled or len(before) != len(item[key])
            if sampled:
                item["coverage"] = "partial"
                item["missing_reason"] = (
                    "Detailed observations outside the configured capture mode/scope were not sampled; the full candidate pool still determined the decision."
                )
                self._failed(item["missing_reason"])
            saved_count = sum(
                len(item.get(key, []))
                for key in ("features", "predicates", "candidates")
            )
            if (
                self.detail_rows_saved + self._pending_rows + saved_count
                > self.config.max_detail_rows
            ):
                self.stage_coverage[stage_id] = "partial"
                self._failed(
                    "Detail collection reached its declared row capacity limit."
                )
                return
            stage = StageObservation.model_validate(item)
            if stage.coverage != "complete":
                self._failed(stage.missing_reason)
            raw = stage.model_dump(mode="json")
            size = len(canonical_bytes(raw))
            if (
                self.saved_bytes + self._pending_bytes + size
                > self.config.max_uncompressed_bytes
            ):
                self.stage_coverage[stage_id] = "partial"
                self._failed(
                    "Detail collection reached its declared byte capacity limit."
                )
                return
            if any(row["stage_id"] == stage_id for row in self._current["stages"]):
                raise ValueError("Duplicate stage emission")
            self._current["stages"].append(raw)
            self._pending_rows += saved_count
            self._pending_bytes += size
            if self.stage_coverage.get(stage_id, "complete") == "complete":
                self.stage_coverage[stage_id] = stage.coverage
        except CaptureLimit:
            self.stage_coverage[stage_id] = "partial"
            self._failed(
                "Stage collection reached its declared byte capacity limit before copying."
            )
        except Exception:  # noqa: BLE001 - capture failure is coverage, never reevaluation
            self.stage_coverage[stage_id] = "partial"
            self._failed(
                "A stage could not be collected; its values were not reevaluated or inferred."
            )

    def end_decision(self):
        current, selected = self._current, self._selected
        self._current, self._selected = None, False
        if current is None or not selected:
            return
        try:
            record = DecisionRecord.model_validate(current)
            if record.pairing_key() in self._event_keys:
                raise ValueError("Duplicate source event identity")
            size = len(canonical_bytes(record))
            if self.saved_bytes + size > self.config.max_uncompressed_bytes:
                self._failed(
                    "Decision collection reached its declared total byte capacity limit."
                )
                return
            if self._on_record is None:
                self.records.append(record)
            else:
                self._on_record(record)
            self._event_keys.add(record.pairing_key())
            self.decisions_saved += 1
            self.detail_rows_saved += self._pending_rows
            self.saved_bytes += size
        except Exception:  # noqa: BLE001 - retain capture failure without altering engine state
            self._failed(
                "A decision could not be finalized; no original execution fact was replaced."
            )

    def summary(self):
        self.end_decision()
        if not self.decisions_seen:
            self._failed(
                "This code has no supported structured decision capture points."
            )
        return {
            "capture": self.config.model_dump(mode="json"),
            "coverage": ("not_collected" if not self.decisions_saved else "partial")
            if self.missing_reasons
            else "complete",
            "missing_reasons": sorted(self.missing_reasons),
            "decisions_seen": self.decisions_seen,
            "decisions_saved": self.decisions_saved,
            "detail_rows_seen": self.detail_rows_seen,
            "detail_rows_saved": self.detail_rows_saved,
            "stage_coverage": dict(self.stage_coverage),
            "source": "strategy_emitted",
            "trust_boundary": "Native strategy and engine share a process. Emitted observations are not an independently trusted ledger or no-lookahead proof.",
        }
