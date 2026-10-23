"""Native holdout staging for a distinct, frozen-selection final job.

This is not an optimizer option. The API/worker must verify the immutable owned
selection and split before calling this boundary; the typed final configuration
then prevents a changed source window or environment from being staged.
"""

from collections.abc import Callable, Mapping

from .final_config import FinalEvaluationConfig
from .windows import (
    DEFAULT_LIMITS,
    CsvLimits,
    StagedDataset,
    _stage_resolved_dataset_window,
)


def stage_final_dataset_window(
    config: FinalEvaluationConfig,
    manifest: Mapping,
    read_file: Callable[[str], bytes],
    *,
    limits: CsvLimits = DEFAULT_LIMITS,
) -> StagedDataset:
    # Revalidate nested models too, including unchecked model_copy mutations.
    config = FinalEvaluationConfig.model_validate(config.model_dump(mode="json"))
    return _stage_resolved_dataset_window(
        config.source_config,
        "holdout",
        config.holdout,
        manifest,
        read_file,
        limits=limits,
    )
