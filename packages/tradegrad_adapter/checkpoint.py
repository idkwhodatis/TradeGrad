"""Complete, content-verified JSON checkpoints. No directory guessing or pickle.

Filesystem publication precedes the caller's fenced database-pointer transaction.
A crash between these operations leaves an orphan, never an implicitly selected
checkpoint. Hashes detect corruption; they do not authenticate a hostile writer.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024
_SAFE_NAME = re.compile(r"[a-zA-Z0-9_.-]+\Z")


class CheckpointError(ValueError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def json_digest(value: Any) -> str:
    return digest(canonical_json(value))


def atomic_json(path: Path, value: Any) -> None:
    """Durably replace a JSON record on its current filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class CheckpointRef:
    manifest_path: str
    manifest_sha256: str
    input_digest: str
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_value(cls, value: CheckpointRef | Mapping[str, Any]) -> CheckpointRef:
        return value if isinstance(value, cls) else cls(**dict(value))


class CheckpointStore:
    def __init__(
        self,
        root: Path | str,
        input_digest: str,
        *,
        fault: Callable[[str], None] | None = None,
    ):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.input_digest = input_digest
        self.fault = fault or (lambda _: None)

    def commit(
        self,
        state: Mapping[str, Any],
        *,
        committed: Callable[[CheckpointRef], None] | None = None,
    ) -> CheckpointRef:
        identifier = uuid.uuid4().hex
        staging = self.root / f".staging-{identifier}"
        published = self.root / identifier
        validate_state(state, expected_input_digest=self.input_digest)
        files = state_artifacts(state)
        if sum(map(len, files.values())) > MAX_CHECKPOINT_BYTES:
            raise CheckpointError("checkpoint exceeds size budget")
        staging.mkdir()
        try:
            inventory = []
            for name, content in sorted(files.items()):
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                inventory.append(
                    {
                        "path": name,
                        "sha256": digest(content),
                        "size_bytes": len(content),
                    }
                )
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "complete": True,
                "input_digest": self.input_digest,
                "files": inventory,
            }
            manifest_bytes = canonical_json(manifest)
            with (staging / "manifest.json").open("xb") as stream:
                stream.write(manifest_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            for directory in sorted(
                (p for p in staging.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                fsync_dir(directory)
            fsync_dir(staging)
            self.fault("before_manifest_publish")
            os.rename(staging, published)
            fsync_dir(self.root)
            self.fault("after_manifest_publish")
            reference = CheckpointRef(
                str(published / "manifest.json"),
                digest(manifest_bytes),
                self.input_digest,
            )
            # Validate our publication before making it eligible for the DB pointer.
            self.load(reference, expected_input_digest=self.input_digest)
            self.fault("before_checkpoint_pointer")
            if committed:
                committed(reference)
            self.fault("after_checkpoint_pointer")
            return reference
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    @staticmethod
    def load(
        reference: CheckpointRef | Mapping[str, Any], *, expected_input_digest: str
    ) -> dict[str, Any]:
        reference = CheckpointRef.from_value(reference)
        if (
            reference.schema_version != SCHEMA_VERSION
            or reference.input_digest != expected_input_digest
        ):
            raise CheckpointError("incompatible checkpoint schema or immutable inputs")
        manifest_path = Path(reference.manifest_path)
        if (
            manifest_path.name != "manifest.json"
            or manifest_path.is_symlink()
            or not manifest_path.is_file()
        ):
            raise CheckpointError("checkpoint must reference a regular manifest file")
        if any(parent.is_symlink() for parent in manifest_path.parents):
            raise CheckpointError("checkpoint path may not traverse symbolic links")
        if manifest_path.stat().st_size > 1024 * 1024:
            raise CheckpointError("manifest exceeds size budget")
        content = manifest_path.read_bytes()
        if digest(content) != reference.manifest_sha256:
            raise CheckpointError("checkpoint manifest hash mismatch")
        try:
            manifest = json.loads(content)
            if (
                manifest["schema_version"] != SCHEMA_VERSION
                or manifest["complete"] is not True
                or manifest["input_digest"] != expected_input_digest
            ):
                raise CheckpointError("incomplete or incompatible checkpoint")
            paths = set()
            total = 0
            state = None
            verified_files = {}
            for entry in manifest["files"]:
                relative = Path(entry["path"])
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or relative.as_posix() in paths
                ):
                    raise CheckpointError(
                        "invalid or duplicate checkpoint artifact path"
                    )
                paths.add(relative.as_posix())
                target = manifest_path.parent / relative
                if (
                    any(p.is_symlink() for p in (target, *target.parents))
                    or not target.is_file()
                ):
                    raise CheckpointError("checkpoint artifact is not a regular file")
                total += target.stat().st_size
                if (
                    total > MAX_CHECKPOINT_BYTES
                    or target.stat().st_size != entry["size_bytes"]
                ):
                    raise CheckpointError("checkpoint artifact size mismatch")
                data = target.read_bytes()
                if digest(data) != entry["sha256"]:
                    raise CheckpointError("checkpoint artifact hash mismatch")
                verified_files[relative.as_posix()] = data
                if relative.as_posix() == "state.json":
                    state = json.loads(data)
            required = {
                "state.json",
                "inputs.json",
                "archive.json",
                "memory.json",
                "lineage.json",
                "random.json",
                "completed_calls.json",
                "next_stage.json",
            }
            if (
                not required <= paths
                or state is None
                or state.get("schema_version") != SCHEMA_VERSION
            ):
                raise CheckpointError("checkpoint is missing required state")
            if json_digest(state["inputs"]) != expected_input_digest:
                raise CheckpointError("checkpoint state input hash mismatch")
            validate_state(state, expected_input_digest=expected_input_digest)
            if verified_files != state_artifacts(state):
                raise CheckpointError(
                    "checkpoint sections disagree with authoritative state"
                )
            return state
        except (
            KeyError,
            TypeError,
            ValueError,
            RecursionError,
            OverflowError,
        ) as error:
            if isinstance(error, CheckpointError):
                raise
            raise CheckpointError("invalid checkpoint JSON schema") from error


def state_artifacts(state: Mapping[str, Any]) -> dict[str, bytes]:
    files: dict[str, bytes] = {
        "state.json": canonical_json(state),
        "inputs.json": canonical_json(state["inputs"]),
        "archive.json": canonical_json(state["optimizer"]["archive"]),
        "memory.json": canonical_json(
            {key: state["optimizer"][key] for key in ("memory_ids", "memory_summary")}
        ),
        "lineage.json": canonical_json(
            [
                {
                    key: candidate.get(key)
                    for key in (
                        "candidate_id",
                        "parent_id",
                        "kind",
                        "iteration",
                        "repair_index",
                    )
                }
                for candidate in state["candidates"]
            ]
        ),
        "random.json": canonical_json(state["random_state"]),
        "completed_calls.json": canonical_json(state["model_calls"]),
        "next_stage.json": canonical_json(
            {"next_stage": state["next_stage"], "iteration": state["iteration"]}
        ),
    }
    for candidate in state["candidates"]:
        name = candidate["candidate_id"]
        if not _SAFE_NAME.fullmatch(name):
            raise CheckpointError("unsafe candidate identifier")
        files[f"candidates/{name}/strategy.py"] = candidate["code"].encode()
        files[f"candidates/{name}/evaluation.json"] = canonical_json(
            candidate.get("evaluation")
        )
    return files


def validate_state(state: Mapping[str, Any], *, expected_input_digest: str) -> None:
    """Constrained JSON state only. This must not import numpy or deserialize code."""

    def check(condition, message):
        if not condition:
            raise CheckpointError(message)

    def integer(value, minimum=0, maximum=2**63 - 1):
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and minimum <= value <= maximum
        )

    def sha(value):
        return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))

    def identifier(value):
        return (
            isinstance(value, str)
            and len(value) <= 160
            and bool(_SAFE_NAME.fullmatch(value))
        )

    try:
        check(
            isinstance(state, dict) and state.get("schema_version") == SCHEMA_VERSION,
            "invalid checkpoint state schema",
        )
        check(
            json_digest(state["inputs"]) == expected_input_digest,
            "checkpoint state input hash mismatch",
        )
        check(
            state["next_stage"]
            in {"evaluate", "repair", "advance", "backward", "generate", "finished"},
            "unknown checkpoint next stage",
        )
        check(integer(state["iteration"], maximum=1000), "invalid checkpoint iteration")
        check(
            isinstance(state["elapsed_seconds"], (int, float))
            and not isinstance(state["elapsed_seconds"], bool)
            and math.isfinite(state["elapsed_seconds"])
            and state["elapsed_seconds"] >= 0,
            "invalid checkpoint elapsed time",
        )
        check(
            state["stop_reason"] is None or isinstance(state["stop_reason"], str),
            "invalid checkpoint stop reason",
        )
        check(
            isinstance(state["feedback"], str)
            and len(state["feedback"].encode()) <= 1024 * 1024,
            "invalid checkpoint feedback",
        )
        candidates, windows = state["candidates"], state["inputs"]["windows"]
        check(
            isinstance(windows, list)
            and 0 < len(windows) <= 202
            and all(
                isinstance(w, dict)
                and w.get("split", "train") in {"train", "validation"}
                for w in windows
            ),
            "invalid checkpoint evaluation windows",
        )
        check(
            isinstance(candidates, list) and 0 < len(candidates) <= 21021,
            "invalid checkpoint candidates",
        )
        by_id = {}
        for candidate in candidates:
            name = candidate["candidate_id"]
            check(
                identifier(name) and name not in by_id,
                "invalid or duplicate candidate identity",
            )
            check(
                candidate["parent_id"] is None or candidate["parent_id"] in by_id,
                "invalid candidate lineage",
            )
            check(
                candidate["kind"] in {"seed", "generated", "repair"},
                "invalid candidate kind",
            )
            check(
                integer(candidate["iteration"], maximum=1000)
                and integer(candidate["repair_index"], maximum=20),
                "invalid candidate iteration or repair index",
            )
            check(
                isinstance(candidate["code"], str)
                and len(candidate["code"].encode()) <= 1024 * 1024
                and sha(candidate["code_sha256"])
                and digest(candidate["code"].encode()) == candidate["code_sha256"],
                "invalid candidate code hash or size",
            )
            check(isinstance(candidate["valid"], bool), "invalid candidate validity")
            outcomes = candidate["window_results"]
            check(
                isinstance(outcomes, list) and len(outcomes) <= len(windows),
                "invalid candidate window results",
            )
            for index, outcome in enumerate(outcomes):
                check(
                    isinstance(outcome, dict)
                    and outcome["window"] == windows[index]
                    and outcome["split"] == windows[index].get("split", "train")
                    and isinstance(outcome["valid"], bool),
                    "candidate window identity mismatch",
                )
            evaluation = candidate["evaluation"]
            check(
                evaluation is None
                or (
                    isinstance(evaluation, dict)
                    and len(outcomes) == len(windows)
                    and evaluation["windows"] == outcomes
                ),
                "incomplete candidate aggregate",
            )
            check(
                not candidate["valid"] or evaluation is not None,
                "unevaluated candidate cannot be valid",
            )
            by_id[name] = candidate
        check(
            candidates[0]["kind"] == "seed"
            and candidates[0]["parent_id"] is None
            and candidates[0]["code_sha256"] == state["inputs"]["initial_code_sha256"],
            "seed hash mismatch",
        )
        check(
            state["current_id"] in by_id
            and by_id[state["current_id"]]["iteration"] == state["iteration"],
            "invalid current candidate",
        )
        current = by_id[state["current_id"]]
        if state["next_stage"] in {"advance", "backward", "generate", "repair"}:
            check(
                current["evaluation"] is not None,
                "stage requires a completed candidate evaluation",
            )
        if state["next_stage"] == "finished":
            check(
                isinstance(state["stop_reason"], str) and bool(state["stop_reason"]),
                "finished checkpoint requires stop reason",
            )
        optimizer = state["optimizer"]
        for key in ("archive", "memory_ids", "sampled_ids"):
            check(
                isinstance(optimizer[key], list)
                and len(optimizer[key]) <= len(candidates)
                and all(name in by_id for name in optimizer[key]),
                "invalid optimizer candidate references",
            )
        check(
            len(optimizer["archive"]) == len(set(optimizer["archive"]))
            and all(
                by_id[name]["evaluation"] is not None for name in optimizer["archive"]
            ),
            "invalid optimizer archive",
        )
        check(
            all(name in optimizer["archive"] for name in optimizer["sampled_ids"]),
            "sampled candidate missing from archive",
        )
        check(
            isinstance(optimizer["memory_summary"], str)
            and len(optimizer["memory_summary"].encode()) <= 1024 * 1024,
            "invalid optimizer memory summary",
        )
        check(
            optimizer["revision_mode"] in {"small", "large"}
            and (
                optimizer["pending_parent_id"] is None
                or optimizer["pending_parent_id"] in by_id
            ),
            "invalid optimizer generation state",
        )
        rng = state["random_state"]
        python, numpy = rng["python"], rng["numpy"]
        check(
            isinstance(python, list)
            and len(python) == 3
            and python[0] == 3
            and isinstance(python[1], list)
            and len(python[1]) == 625
            and all(integer(value, maximum=2**32 - 1) for value in python[1][:-1])
            and integer(python[1][-1], maximum=624)
            and (python[2] is None or isinstance(python[2], (int, float))),
            "invalid Python random state",
        )
        check(
            isinstance(numpy, list)
            and len(numpy) == 5
            and numpy[0] == "MT19937"
            and isinstance(numpy[1], list)
            and len(numpy[1]) == 624
            and all(integer(value, maximum=2**32 - 1) for value in numpy[1])
            and integer(numpy[2], maximum=624)
            and integer(numpy[3], maximum=1)
            and isinstance(numpy[4], (int, float)),
            "invalid numpy random state",
        )
        calls = state["model_calls"]
        check(
            isinstance(calls, list) and len(calls) <= 10000,
            "invalid checkpoint model call count",
        )
        names = set()
        for index, call in enumerate(calls):
            check(
                isinstance(call, dict)
                and isinstance(call["call_id"], str)
                and len(call["call_id"]) <= 200
                and call["call_id"] not in names
                and call["logical_index"] == index,
                "invalid model call identity or order",
            )
            names.add(call["call_id"])
            check(
                call["input_digest"] == expected_input_digest
                and sha(call["request_sha256"]),
                "model call input hash mismatch",
            )
            check(
                call["status"] in {"started", "completed", "failed"}
                and isinstance(call["adopted"], bool)
                and integer(call["charged_tokens"]),
                "invalid model call outcome",
            )
            check(
                call["status"] != "completed" or isinstance(call["response"], str),
                "missing completed model response",
            )
            check(
                all(
                    call[name] is None or integer(call[name])
                    for name in ("input_tokens", "output_tokens")
                ),
                "invalid model token usage",
            )
        # Explicitly reject non-finite JSON numbers anywhere, including outcomes
        # and provider metadata, even though Python json.loads accepts NaN.
        canonical_json(state)
    except (
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        RecursionError,
        OverflowError,
    ) as error:
        if isinstance(error, CheckpointError):
            raise
        raise CheckpointError("invalid checkpoint state schema") from error
