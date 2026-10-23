"""Prove non-result artifact contents instead of trusting a filename exemption."""

import hashlib
import json
import re

from backend.app.models import Candidate, ExecutionAttempt, StrategyVersion
from backend.app.research_services import bounded_json
from backend.app.services import DomainError
from packages.tradegrad_adapter.config import RunConfig
from packages.tradegrad_adapter.final_config import FinalEvaluationConfig
from packages.tradegrad_adapter.strategy_package import (
    MAX_ENVIRONMENT_BYTES,
    NATIVE_ENV_SCHEMA,
    PORTABLE_ENV_SCHEMA,
    validate_environment_evidence,
)

METADATA_NAMES = {"execution-environment.json", "provenance.json", "request.json"}
SOURCE_NAMES = {"strategy.py"}


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate metadata key")
        value[key] = item
    return value


def _source_matches(session, run, evaluation, artifact, content):
    """Source is exempt only when its exact bytes are already owned source text."""
    if not 0 < len(content) <= 1_048_576:
        return False
    final = run.config.get("execution_mode") == "final_evaluation"
    if evaluation is not None and evaluation.candidate_id is not None and not final:
        source = session.get(Candidate, evaluation.candidate_id)
        if (
            source is None
            or source.run_id != run.id
            or source.id != evaluation.candidate_id
            or source.code_sha256 != evaluation.code_sha256
        ):
            return False
        expected = source.code_sha256
    else:
        source = session.get(StrategyVersion, run.strategy_version_id)
        if (
            source is None
            or source.owner_id != run.owner_id
            or source.id != run.strategy_version_id
        ):
            return False
        expected = source.content_sha256
        ref = run.config.get("selected_strategy" if final else "seed_strategy")
        if ref and (ref.get("id") != str(source.id) or ref.get("sha256") != expected):
            return False
        if evaluation is not None and evaluation.code_sha256 != expected:
            return False
    return (
        isinstance(source.code, str)
        and content == source.code.encode("utf-8")
        and hashlib.sha256(content).hexdigest() == expected == artifact.sha256
    )


def _native_request(run, evaluation):
    """Reconstruct the reviewed native staging input, never arbitrary JSON."""
    schema = run.config.get("schema_version")
    if schema is None:
        return {
            key: run.config[key]
            for key in ("task", "market", "profile", "start_date", "end_date")
        }
    if schema == "tradegrad.final-evaluation.v1":
        config = FinalEvaluationConfig.model_validate(run.config)
        source = config.source_config
        window = {"split": "holdout", **config.holdout.model_dump(mode="json")}
    elif schema == "tradegrad.run.v1" and evaluation is not None:
        config = source = RunConfig.model_validate(run.config)
        window = evaluation.window
    else:
        # Portable engine request artifacts are not published by the worker.
        # Do not grant a speculative exemption for another input format.
        raise ValueError("Request has no proven native input binding")
    return {
        "task": source.task,
        "market": source.market_profile.market,
        "profile": source.profile,
        "start_date": window["start"],
        "end_date": window["end"],
        "evaluation_config": {
            "engine_overrides": source.market_profile.engine_kwargs(),
            "window": {
                "start": window["start"],
                "end": window["end"],
                "role": window["split"],
            },
            "warmup": {
                "sessions": source.warmup.sessions,
                "history_policy": source.warmup.history_policy,
            },
            "benchmark": None,
            "run_config_sha256": config.sha256,
        },
    }


def _provenance_image(session, run, evaluation, artifact):
    if evaluation is not None:
        return evaluation.image_digest
    if run.config.get("schema_version") is not None:
        return run.research_image_digest
    # Legacy seed runs predate research-image pinning. Their committed result is
    # the persisted image authority, but only for the exact finished generation.
    # Never borrow a later retry's image for an older attempt or accept a filename
    # as proof. Unknown/failed legacy provenance remains unavailable, not guessed.
    attempt = session.get(ExecutionAttempt, artifact.attempt_id)
    result = run.result
    if (
        attempt is None
        or attempt.id != artifact.attempt_id
        or attempt.run_id != run.id
        or run.active_attempt_id != attempt.id
        or attempt.generation != run.generation
        or run.status != "succeeded"
        or attempt.status != "succeeded"
        or attempt.exit_code != 0
        or not attempt.sandbox_stopped
        or attempt.finished_at is None
        or not isinstance(result, dict)
        or result.get("execution_mode") != "seed_evaluation"
        or result.get("evaluation_mode") != "native_akquant"
        or result.get("result_trust") != "native_strategy_and_engine_share_process"
        or not isinstance(result.get("image_digest"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", result["image_digest"])
    ):
        raise ValueError("Legacy provenance lacks its exact committed image binding")
    return result["image_digest"]


def validate_non_result_artifact(session, run, evaluation, artifact, suffix, content):
    """Known metadata/source must have both a bounded shape and an owned binding.

    Unknown or result-shaped bytes under a reserved metadata name are rejected,
    including on cancelled finals. Source text is never executed or treated as
    metric exposure merely because its strings mention result field names.
    """
    try:
        if len(content) != artifact.size_bytes:
            raise ValueError("Metadata registration size differs")
        if suffix in SOURCE_NAMES:
            if not _source_matches(session, run, evaluation, artifact, content):
                raise ValueError("Source differs from its owned immutable binding")
            return
        bound = (
            MAX_ENVIRONMENT_BYTES if suffix == "execution-environment.json" else 256_000
        )
        if artifact.media_type != "application/json" or not 0 < len(content) <= bound:
            raise ValueError("Metadata is not bounded JSON")
        value = json.loads(content, object_pairs_hook=_object)
        bounded_json(value, bound)
        if suffix == "provenance.json":
            expected = {
                "schema_version": 1,
                "execution_mode": run.config.get("execution_mode", "seed_evaluation"),
                "evaluation_mode": "native_akquant",
                "result_trust": "native_strategy_and_engine_share_process",
                "image_digest": _provenance_image(session, run, evaluation, artifact),
            }
            if value != expected or type(value.get("schema_version")) is not int:
                raise ValueError("Provenance differs from the fixed publisher shape")
        elif suffix == "execution-environment.json":
            portable = run.config.get("schema_version") in {
                "tradegrad.portable.run.v1",
                "tradegrad.portable.final-evaluation.v1",
            }
            expected_schema = PORTABLE_ENV_SCHEMA if portable else NATIVE_ENV_SCHEMA
            if (
                not isinstance(value, dict)
                or value.get("schema_version") != expected_schema
            ):
                raise ValueError("Environment metadata family differs")
            images = (
                {
                    "engine": run.research_image_digest,
                    "policy": run.portable_policy_image_digest,
                }
                if portable
                else run.research_image_digest
            )
            validate_environment_evidence(
                value, run.config["environment_digest"], images
            )
        elif suffix == "request.json":
            if value != _native_request(run, evaluation):
                raise ValueError("Request differs from the reviewed input binding")
        else:
            raise ValueError("Unknown metadata shape")
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
        DomainError,
    ):
        raise DomainError(
            "artifact_metadata_invalid",
            "Artifact does not match its bounded metadata or owned source binding",
            409,
        ) from None
