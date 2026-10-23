"""Recoverable orchestration of the actual upstream TradeTGD algorithm.

No generated module is imported or executed here. The supplied evaluator must
cross the production fail-closed isolation boundary. Only the research runtime
loads numpy/OpenAI dependencies, lazily through the upstream optimizer.

Deliberate differences from the legacy run_one.py orchestration:
* immutable repairs and evidence instead of directory deletion;
* deterministic sequential memory requests, retaining the same source prompts;
* explicit JSON state/RNG and logical-call replay, not directory-number resume;
* bounded calls/runtime/repairs and no-valid-candidate termination;
* validation windows are reported but never enter the source training reward;
* invalid strategies stay in the source archive (including its zero-on-error
  window reward), but cannot be returned as a usable best strategy;
* performance key order is canonicalized before source prompt formatting so a
  JSON round trip cannot silently change logical requests;
* hard model failures/unknown outcomes stop instead of hidden network retries,
  and fallback lineage points at the actual unchanged base.
"""

from __future__ import annotations

import contextlib
import json
import math
import random
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .checkpoint import (
    CheckpointRef,
    CheckpointStore,
    atomic_json,
    canonical_json,
    digest,
    fsync_dir,
    json_digest,
)
from .model_calls import (
    ModelCallAbort,
    ModelCallBoundaryFailure,
    ModelProvider,
    OptimizerStop,
    RecordedModel,
    sanitize,
)

_RANDOM_LOCK = threading.RLock()
Event = Callable[[str, Mapping[str, Any]], None]
Evaluator = Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]]


def immutable_input_digest(
    config: Mapping[str, Any], initial_code: str, windows: Sequence[Mapping[str, Any]]
) -> str:
    return json_digest(_inputs(config, initial_code, windows))


def _inputs(
    config: Mapping[str, Any], initial_code: str, windows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "config": dict(config),
        "initial_code_sha256": digest(initial_code.encode()),
        "windows": [dict(w) for w in windows],
    }


@dataclass
class OptimizationResult:
    candidates: list[dict[str, Any]]
    best_candidate_id: str | None
    stop_reason: str
    checkpoint: CheckpointRef
    model_calls: list[dict[str, Any]]
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "best_candidate_id": self.best_candidate_id,
            "stop_reason": self.stop_reason,
            "checkpoint": self.checkpoint.to_dict(),
            "model_calls": self.model_calls,
            "status": self.status,
        }


@dataclass(eq=False)
class _StrategyRecord:
    """Data-only Strategy duck type: source Strategy.__init__ writes code in place."""

    id: str
    code: str
    strategy_dir: Path
    performance: dict[str, Any]
    parent_id: str | None = None


def _source_optimizer(
    llm: RecordedModel, settings: Mapping[str, Any], *, portable=False
):
    from src.optimizer import TradeTGD
    from src.prompt import (
        MEMORY_GLOBAL_SUMMARY_SYSTEM_PROMPT,
        MEMORY_GLOBAL_SUMMARY_USER_PROMPT,
    )

    class SequentialTradeTGD(TradeTGD):
        def update_memory_summary(self):
            # Only concurrency changes: source summary prompts and FIFO cadence are
            # unchanged, giving stable call order and preventing nested pools.
            summaries = [
                self.summarize_strategy(strategy) for strategy in self.memory_storage
            ]
            self.memory_summary = self.llm.query(
                MEMORY_GLOBAL_SUMMARY_USER_PROMPT.format(
                    individual_summaries="\n\n".join(summaries),
                    previous_summary=self.memory_summary,
                ),
                MEMORY_GLOBAL_SUMMARY_SYSTEM_PROMPT,
            ).strip()

    if portable:
        from .portable_prompts import portable_step_method

        SequentialTradeTGD.step = portable_step_method(TradeTGD.step)

    return SequentialTradeTGD(
        llm,
        sample_size=int(settings.get("sample_size", 3)),
        sample_method={"top_k": "topk"}.get(
            settings.get("sample_method"), settings.get("sample_method", "power_law")
        ),
        large_revision_prob=float(settings.get("large_revision_prob", 0.7)),
        memory_window=int(settings.get("memory_window", 5)),
        verbose=False,
    )


def _random_snapshot() -> dict[str, Any]:
    import numpy as np

    python = random.getstate()
    numpy = np.random.get_state()
    return {
        "python": [python[0], list(python[1]), python[2]],
        "numpy": [
            numpy[0],
            numpy[1].tolist(),
            int(numpy[2]),
            int(numpy[3]),
            float(numpy[4]),
        ],
    }


def _random_restore(state: Mapping[str, Any]) -> None:
    import numpy as np

    python, numpy = state["python"], state["numpy"]
    random.setstate((python[0], tuple(python[1]), python[2]))
    np.random.set_state(
        (numpy[0], np.array(numpy[1], dtype="uint32"), numpy[2], numpy[3], numpy[4])
    )


@contextlib.contextmanager
def _owned_random(state: dict[str, Any]):
    # Upstream uses module-global RNGs. Serialize adapter invocations and restore
    # ambient state, avoiding bleed between two runs sharing a research process.
    with _RANDOM_LOCK:
        ambient = _random_snapshot()
        try:
            _random_restore(state["random_state"])
            yield
        finally:
            state["random_state"] = _random_snapshot()
            _random_restore(ambient)


def _initial_random(seed: int) -> dict[str, Any]:
    import numpy as np

    with _RANDOM_LOCK:
        ambient = _random_snapshot()
        try:
            random.seed(seed)
            np.random.seed(seed)
            return _random_snapshot()
        finally:
            _random_restore(ambient)


def _optimizer_snapshot(optimizer) -> dict[str, Any]:
    return {
        "archive": [s.id for s in optimizer.database],
        "memory_ids": [s.id for s in optimizer.memory_storage],
        "memory_summary": optimizer.memory_summary,
        "sampled_ids": [s.id for s in getattr(optimizer, "sampled_strategies", [])],
        "revision_mode": optimizer.current_revision_mode,
        "pending_parent_id": optimizer.pending_parent_id,
    }


def _optimizer_restore(optimizer, state: Mapping[str, Any], root: Path) -> None:
    candidates = {
        c["candidate_id"]: _StrategyRecord(
            c["candidate_id"],
            c["code"],
            root / "candidates" / c["candidate_id"],
            json.loads(canonical_json(c.get("evaluation") or {})),
            c.get("parent_id"),
        )
        for c in state["candidates"]
    }
    archive = state["optimizer"]
    optimizer.database = [candidates[key] for key in archive["archive"]]
    optimizer.memory_storage = [candidates[key] for key in archive["memory_ids"]]
    optimizer.memory_summary = archive["memory_summary"]
    optimizer.sampled_strategies = [candidates[key] for key in archive["sampled_ids"]]
    optimizer.current_revision_mode = archive["revision_mode"]
    optimizer.pending_parent_id = archive["pending_parent_id"]


def _candidate(
    identifier: str,
    code: str,
    parent: str | None,
    kind: str,
    iteration: int,
    repair: int = 0,
) -> dict[str, Any]:
    return {
        "candidate_id": identifier,
        "parent_id": parent,
        "kind": kind,
        "iteration": iteration,
        "repair_index": repair,
        "code": code,
        "code_sha256": digest(code.encode()),
        "evaluation": None,
        "window_results": [],
        "valid": False,
    }


def _publish_code(
    root: Path, candidate: Mapping[str, Any], fault: Callable[[str], None]
) -> None:
    fault("before_code_write")
    directory = root / "candidates" / candidate["candidate_id"]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "strategy.py"
    if path.exists():
        if path.is_symlink() or digest(path.read_bytes()) != candidate["code_sha256"]:
            raise ValueError(
                "immutable candidate code already exists with different content"
            )
    else:
        # Publish complete bytes atomically without replacing immutable evidence.
        # A process killed mid-write leaves only an ignored temporary file.
        import os
        import uuid

        temporary = directory / f".strategy-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(candidate["code"].encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
            fsync_dir(directory)
        finally:
            temporary.unlink(missing_ok=True)
    fault("after_code_write")


def _normalized_outcome(
    outcome: Mapping[str, Any], window: Mapping[str, Any]
) -> dict[str, Any]:
    result = dict(outcome)
    if "metrics" in result:
        metrics = result["metrics"]
        if isinstance(metrics, Mapping):
            result = {**dict(metrics), **result}
    score = result.get("combined_score")
    valid = (
        isinstance(score, (int, float))
        and not isinstance(score, bool)
        and math.isfinite(score)
        and not result.get("error")
        and result.get("status", "succeeded") == "succeeded"
        and result.get("exit_code", 0) == 0
        and not result.get("timed_out", False)
        and not result.get("cancelled", False)
    )
    if not valid:
        result["error"] = sanitize(
            str(
                result.get("error")
                or "evaluation returned no valid finite combined_score"
            )
        )[:8192]
        result["combined_score"] = 0
    result.update(
        window=dict(window), split=window.get("split", "train"), valid=bool(valid)
    )
    # Ensure no unsafe object or non-finite values cross into JSON persistence.
    canonical_json(result)
    return result


def _aggregate(
    outcomes: Sequence[Mapping[str, Any]], worst_ratio: float
) -> dict[str, Any]:
    train = [p for p in outcomes if p["split"] == "train"]
    scores = [p.get("combined_score", 0) for p in train]
    n_worst = max(1, math.ceil(len(scores) * worst_ratio))
    result = {
        "combined_score": round(sum(sorted(scores)[:n_worst]) / n_worst, 2),
        "windows": list(outcomes),
    }
    errors = [p["error"] for p in train if p.get("error")]
    if errors:
        result["error"] = "\n\n".join(errors)
    validation = [p for p in outcomes if p["split"] == "validation"]
    if validation:
        result["validation_valid"] = all(p["valid"] for p in validation)
        if not result["validation_valid"]:
            result["validation_error"] = "\n\n".join(
                p["error"] for p in validation if p.get("error")
            )
        result["validation_score"] = (
            round(sum(p["combined_score"] for p in validation) / len(validation), 2)
            if result["validation_valid"]
            else None
        )
    return result


def run_optimization(
    *,
    config: Mapping[str, Any],
    initial_code: str,
    windows: Sequence[Mapping[str, Any]],
    evaluate: Evaluator,
    root: Path | str,
    model: ModelProvider,
    event: Event | None = None,
    checkpoint_committed: Callable[[CheckpointRef], None] | None = None,
    control: Callable[[], str | None] | None = None,
    resume: CheckpointRef | Mapping[str, Any] | None = None,
    fault: Callable[[str], None] | None = None,
) -> OptimizationResult:
    """Run max_iterations revisions (plus seed), with no automatic holdout access.

    ``evaluate`` must synchronously return a JSON mapping with a finite
    ``combined_score`` or an ``error``. It may additionally return ResultBundle
    references and metadata; they are retained. Callback exceptions become failed
    evaluation evidence; a hard process kill/fault (BaseException) propagates.
    ``control`` runs after complete checkpoints and returns pause/cancel/None.
    Fencing, DB transactions and container exit confirmation belong to the worker.
    """
    root = Path(root).absolute()
    root.mkdir(parents=True, exist_ok=True)
    event = event or (lambda *_: None)
    control = control or (lambda: None)
    fault = fault or (lambda _: None)
    config = dict(config)
    portable_seed = (
        config.get("schema_version") == "tradegrad.portable.run.v1"
        and config.get("execution_mode") == "seed_evaluation"
        and config.get("optimizer", {}).get("max_iterations") == 0
    )
    if (
        config.get("execution_mode", "optimization") != "optimization"
        and not portable_seed
    ):
        raise ValueError(
            "optimizer requires execution_mode optimization; use max_iterations=0 for seed-only research"
        )
    windows = [dict(window) for window in windows]
    if (
        not windows
        or any(w.get("split", "train") not in {"train", "validation"} for w in windows)
        or not any(w.get("split", "train") == "train" for w in windows)
    ):
        raise ValueError(
            "optimization requires training windows and forbids holdout evaluation"
        )
    settings, budgets = dict(config["optimizer"]), dict(config["budgets"])
    if settings.get("algorithm", "trade_tgd") != "trade_tgd":
        raise ValueError("unsupported optimization algorithm")
    seed_reference = config.get("seed_strategy")
    if seed_reference is not None and seed_reference.get("sha256") != digest(
        initial_code.encode()
    ):
        raise ValueError("seed strategy bytes differ from immutable configuration")
    if (
        int(settings["max_iterations"]) < 0
        or int(settings.get("memory_window", 5)) <= 0
        or not 0 < float(settings.get("worst_ratio", 0.3)) <= 1
    ):
        raise ValueError("invalid optimizer settings")
    if any(
        float(budgets[key]) < 0
        for key in (
            "max_model_calls",
            "max_tokens",
            "max_runtime_seconds",
            "max_repairs",
        )
    ):
        raise ValueError("negative optimization budget")
    inputs = _inputs(config, initial_code, windows)
    input_digest = json_digest(inputs)
    checkpoint_store = CheckpointStore(root / "checkpoints", input_digest, fault=fault)
    replay_roots: list[Path] = []
    if resume:
        reference = CheckpointRef.from_value(resume)
        state = checkpoint_store.load(reference, expected_input_digest=input_digest)
        # This is the explicitly referenced checkpoint's sibling journal, not a
        # scan for the newest checkpoint. Every record is input-hash verified.
        replay_roots = [
            Path(reference.manifest_path).parent.parent.parent / "model_calls"
        ]
        checkpoint: CheckpointRef | None = reference
    else:
        state = {
            "schema_version": 1,
            "inputs": inputs,
            "next_stage": "evaluate",
            "iteration": 0,
            "current_id": "candidate-0000",
            "candidates": [_candidate("candidate-0000", initial_code, None, "seed", 0)],
            "optimizer": {
                "archive": [],
                "memory_ids": [],
                "memory_summary": "",
                "sampled_ids": [],
                "revision_mode": "small",
                "pending_parent_id": None,
            },
            "random_state": _initial_random(int(settings.get("seed", 0))),
            "model_calls": [],
            "feedback": "",
            "elapsed_seconds": 0.0,
            "stop_reason": None,
        }
        checkpoint = None
    elapsed_base = float(state.get("elapsed_seconds", 0))
    session_start = time.monotonic()
    elapsed = lambda: elapsed_base + time.monotonic() - session_start
    model_settings = dict(config.get("model", {}))
    recorded = RecordedModel(
        root / "model_calls",
        input_digest=input_digest,
        provider=model,
        budgets=budgets,
        prompt_version=str(
            config.get(
                "prompt_version",
                config.get("prompt_template_version", "tradegrad-source.v1"),
            )
        ),
        parameters={
            "temperature": model_settings.get("temperature", 0),
            "max_output_tokens": model_settings.get("max_output_tokens", 4096),
        },
        elapsed=elapsed,
        fault=fault,
        event=event,
        replay_roots=replay_roots,
        initial_records=state["model_calls"],
    )
    elapsed_base = max(
        elapsed_base,
        max(
            (float(row.get("elapsed_seconds", 0)) for row in recorded.records),
            default=0.0,
        ),
    )
    query_model = recorded
    if config.get("schema_version") == "tradegrad.portable.run.v1":
        from .portable_prompts import PROMPT_VERSION, PortablePromptBoundary

        if config.get("prompt_version") != PROMPT_VERSION:
            raise ValueError("Unsupported portable prompt version")
        query_model = PortablePromptBoundary(recorded)
    optimizer = _source_optimizer(
        query_model,
        settings,
        portable=config.get("schema_version") == "tradegrad.portable.run.v1",
    )
    _optimizer_restore(optimizer, state, root)

    def commit() -> None:
        nonlocal checkpoint
        state["optimizer"] = _optimizer_snapshot(optimizer)
        state["model_calls"] = recorded.records
        state["elapsed_seconds"] = elapsed()
        checkpoint = checkpoint_store.commit(state, committed=checkpoint_committed)
        event(
            "checkpoint_committed",
            {
                **checkpoint.to_dict(),
                "next_stage": state["next_stage"],
                "iteration": state["iteration"],
            },
        )

    def record_strategy(candidate: Mapping[str, Any]) -> _StrategyRecord:
        return _StrategyRecord(
            candidate["candidate_id"],
            candidate["code"],
            root / "candidates" / candidate["candidate_id"],
            json.loads(canonical_json(candidate["evaluation"] or {})),
            candidate.get("parent_id"),
        )

    def best_id() -> str | None:
        by_id = {c["candidate_id"]: c for c in state["candidates"]}
        return next((s.id for s in optimizer.database if by_id[s.id]["valid"]), None)

    try:
        if not resume:
            _publish_code(root, state["candidates"][0], fault)
            event("candidate_created", dict(state["candidates"][0]))
            commit()
        else:
            if state["next_stage"] != "finished":
                # A paused checkpoint resumes without changing immutable config.
                state["stop_reason"] = None
            # Bind the authoritative DB pointer to this attempt's journal before
            # doing work. Otherwise a B crash while resuming A would leave C
            # looking only in A and potentially repeating B's charged call.
            # Even finished state must migrate: the previous worker may have died
            # after its final checkpoint but before fenced DB finalization. Keep
            # its stop reason and skip all optimization/evaluation/provider work.
            commit()
        while state["next_stage"] != "finished":
            action = control()
            if action in {"pause", "cancel"}:
                raise OptimizerStop("paused" if action == "pause" else "cancelled")
            if elapsed() >= float(budgets["max_runtime_seconds"]):
                raise OptimizerStop("max_runtime_seconds")
            candidate = next(
                c
                for c in state["candidates"]
                if c["candidate_id"] == state["current_id"]
            )
            stage = state["next_stage"]
            event(
                "stage_changed",
                {
                    "stage": stage,
                    "iteration": state["iteration"],
                    "candidate_id": candidate["candidate_id"],
                },
            )
            if stage == "evaluate":
                _publish_code(root, candidate, lambda _: None)
                for index in range(len(candidate["window_results"]), len(windows)):
                    if elapsed() >= float(budgets["max_runtime_seconds"]):
                        raise OptimizerStop("max_runtime_seconds")
                    event(
                        "budget_updated",
                        {
                            "elapsed_seconds": elapsed(),
                            "remaining_runtime_seconds": max(
                                0.0, float(budgets["max_runtime_seconds"]) - elapsed()
                            ),
                            "model_calls": len(recorded.records),
                            "tokens": recorded.tokens_used,
                        },
                    )
                    window = windows[index]
                    identity = json_digest(
                        {
                            "input_digest": input_digest,
                            "candidate_id": candidate["candidate_id"],
                            "code_sha256": candidate["code_sha256"],
                            "window": window,
                        }
                    )
                    path = (
                        root
                        / "candidates"
                        / candidate["candidate_id"]
                        / f"window-{index:04d}.json"
                    )
                    fault("before_evaluation")
                    if path.exists():
                        saved = json.loads(path.read_bytes())
                        if saved["identity"] != identity or saved[
                            "sha256"
                        ] != json_digest(saved["outcome"]):
                            raise ValueError("evaluation replay hash mismatch")
                        outcome = saved["outcome"]
                    else:
                        try:
                            raw = evaluate(
                                candidate["code"], candidate["candidate_id"], window
                            )
                            outcome = _normalized_outcome(raw, window)
                        except Exception as error:  # noqa: BLE001 - retain arbitrary evaluator failures as evidence
                            outcome = _normalized_outcome(
                                {
                                    "error": f"{type(error).__name__}: {sanitize(str(error), recorded.secrets)}"
                                },
                                window,
                            )
                        fault("after_evaluation")
                        atomic_json(
                            path,
                            {
                                "identity": identity,
                                "outcome": outcome,
                                "sha256": json_digest(outcome),
                            },
                        )
                    candidate["window_results"].append(outcome)
                    event(
                        "evaluation_completed",
                        {
                            "candidate_id": candidate["candidate_id"],
                            "window_index": index,
                            "outcome": outcome,
                        },
                    )
                    commit()
                    action = control()
                    if action in {"pause", "cancel"}:
                        raise OptimizerStop(
                            "paused" if action == "pause" else "cancelled"
                        )
                candidate["evaluation"] = _aggregate(
                    candidate["window_results"], float(settings.get("worst_ratio", 0.3))
                )
                candidate["valid"] = not bool(
                    candidate["evaluation"].get("error")
                ) and candidate["evaluation"].get("validation_valid", True)
                state["next_stage"] = (
                    "repair"
                    if not candidate["valid"]
                    and candidate["repair_index"] < int(budgets["max_repairs"])
                    else "advance"
                )
                if not candidate["valid"] and candidate["repair_index"] >= int(
                    budgets["max_repairs"]
                ):
                    candidate["repair_stop_reason"] = "max_repairs"
                commit()
            elif stage == "repair":
                if config.get("schema_version") == "tradegrad.portable.run.v1":
                    from .portable_prompts import parse_portable_code as parse_code
                else:
                    from src.util import parse_code

                recorded.set_context(f"repair:{candidate['candidate_id']}")
                prompt = f"""Debug the code based on the error, and output a corrected version of full code within ```python ... ``` code block.
# Code
```python
{candidate["code"]}
```
# Error
```
{candidate["evaluation"].get("error") or candidate["evaluation"].get("validation_error")}
```
# Note
- You can only modify the code within `### EDIT START` and `### EDIT END` markers. Do not change anything outside these markers.
- Prioritize vectorized operations and efficient data handling when making improvements."""
                try:
                    code = parse_code(query_model.query(prompt))
                except ValueError as error:
                    candidate["repair_error"] = (
                        f"{type(error).__name__}: {sanitize(str(error), recorded.secrets)}"
                    )
                    state["next_stage"] = "advance"
                    commit()
                    continue
                repair = candidate["repair_index"] + 1
                child = _candidate(
                    f"candidate-{state['iteration']:04d}-repair-{repair:02d}",
                    code,
                    candidate["candidate_id"],
                    "repair",
                    state["iteration"],
                    repair,
                )
                _publish_code(root, child, fault)
                recorded.mark_adopted(
                    [f"{recorded.context}:{recorded.ordinal - 1:04d}"]
                )
                state["candidates"].append(child)
                state["current_id"], state["next_stage"] = (
                    child["candidate_id"],
                    "evaluate",
                )
                event("candidate_created", dict(child))
                commit()
            elif stage == "advance":
                if state["iteration"] >= int(settings["max_iterations"]):
                    optimizer.update_database(record_strategy(candidate))
                    state["next_stage"] = "finished"
                    state["stop_reason"] = (
                        "max_iterations" if best_id() else "all_candidates_invalid"
                    )
                else:
                    state["next_stage"] = "backward"
                commit()
            elif stage == "backward":
                recorded.set_context(f"iteration:{state['iteration']:04d}:backward")
                with _owned_random(state):
                    state["feedback"] = optimizer.backward(
                        record_strategy(candidate), state["iteration"]
                    )
                recorded.mark_adopted(
                    [
                        row["call_id"]
                        for row in recorded.records
                        if row["call_id"].startswith(recorded.context + ":")
                    ]
                )
                state["next_stage"] = "generate"
                commit()
            elif stage == "generate":
                recorded.set_context(f"iteration:{state['iteration']:04d}:generate")
                with _owned_random(state):
                    adopted = True
                    try:
                        code = optimizer.step(state["feedback"])
                    except Exception as error:  # noqa: BLE001 - source safe_step fallback contract
                        adopted = False
                        # Preserve source safe_step's unchanged-base fallback, while
                        # fixing its stale pending_parent_id when another base wins.
                        base = optimizer._select_base()
                        optimizer.pending_parent_id = base.id
                        code = base.code
                        event(
                            "generation_failed",
                            {
                                "error_class": type(error).__name__,
                                "fallback_parent_id": base.id,
                            },
                        )
                number = state["iteration"] + 1
                child = _candidate(
                    f"candidate-{number:04d}",
                    code,
                    optimizer.pending_parent_id,
                    "generated",
                    number,
                )
                _publish_code(root, child, fault)
                if recorded.ordinal and adopted:
                    recorded.mark_adopted(
                        [f"{recorded.context}:{recorded.ordinal - 1:04d}"]
                    )
                state["candidates"].append(child)
                state["current_id"], state["iteration"], state["next_stage"] = (
                    child["candidate_id"],
                    number,
                    "evaluate",
                )
                event("candidate_created", dict(child))
                commit()
            else:
                raise ValueError(f"unsupported optimizer next_stage: {stage}")
    except ModelCallAbort as stop:
        # We have left upstream retry decorators. Let the worker's ordinary
        # Exception cleanup run, but never publish a partially advanced optimizer
        # or claim an unpersisted response completed. Resume the last DB pointer.
        raise ModelCallBoundaryFailure(stop.reason) from stop
    except OptimizerStop as stop:
        state["stop_reason"] = stop.reason
        if stop.reason not in {"paused", "cancelled"}:
            state["next_stage"] = "finished"
        # A successfully evaluated current candidate is usable even when the next
        # model request exhausts budget before backward archives it.
        if stop.reason not in {"paused", "cancelled"}:
            current = next(
                c
                for c in state["candidates"]
                if c["candidate_id"] == state["current_id"]
            )
            if current.get("evaluation") is not None and current[
                "candidate_id"
            ] not in {s.id for s in optimizer.database}:
                optimizer.update_database(record_strategy(current))
        commit()
    assert checkpoint is not None
    reason = state["stop_reason"] or "max_iterations"
    normal_stops = {
        "max_iterations",
        "max_model_calls",
        "max_tokens",
        "max_runtime_seconds",
    }
    status = (
        reason
        if reason in {"paused", "cancelled"}
        else ("succeeded" if best_id() and reason in normal_stops else "failed")
    )
    result = OptimizationResult(
        state["candidates"], best_id(), reason, checkpoint, recorded.records, status
    )
    event(
        "run_finished",
        {
            "status": status,
            "stop_reason": reason,
            "best_candidate_id": result.best_candidate_id,
        },
    )
    return result
