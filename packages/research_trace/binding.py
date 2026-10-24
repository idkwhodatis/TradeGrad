"""One deterministic leaf binding shared by the publisher and verified readers.

The caller proves ownership, result identity and containment. This module only
combines those already-proved values, without database or engine dependencies.
"""

import hashlib

from .contracts import EvaluationBinding, canonical_bytes


def execution_dependencies_sha256(
    *, research_image_digest, portable_policy_image_digest, environment_digest
):
    return hashlib.sha256(
        canonical_bytes(
            {
                "research_image_digest": research_image_digest,
                "portable_policy_image_digest": portable_policy_image_digest,
                "environment_digest": environment_digest,
            }
        )
    ).hexdigest()


def make_binding(*, run, evaluation, config, bundle, result_sha256, candidate_id=None):
    """Bind to the physical Evaluation, not its parent optimizer or logical key.

    A candidate ID can legitimately be absent before its checkpoint commits.
    Exact immutable code and Evaluation IDs remain mandatory in that case.
    """
    source = getattr(config, "source_config", config)
    portable = config.schema_version.startswith("tradegrad.portable.")
    return EvaluationBinding(
        run_id=run.id,
        execution_attempt_id=evaluation.attempt_id,
        evaluation_id=evaluation.id,
        candidate_id=candidate_id,
        strategy_version_id=run.strategy_version_id,
        code_sha256=evaluation.code_sha256,
        dataset_version_id=source.dataset.id,
        dataset_sha256=source.dataset.sha256,
        universe_version_id=source.universe.id,
        universe_sha256=source.universe.sha256,
        execution_config_sha256=bundle["identity"][
            "run_config_sha256" if portable else "evaluation_config_sha256"
        ],
        engine_dependencies_sha256=execution_dependencies_sha256(
            research_image_digest=run.research_image_digest,
            portable_policy_image_digest=run.portable_policy_image_digest,
            environment_digest=source.environment_digest,
        ),
        result_manifest_sha256=result_sha256,
    )
