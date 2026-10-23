"""Synthetic final-result boundary evidence, never an engine execution claim."""

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from backend.tests.research_view_fixtures import synthetic_result
from packages.tradegrad_adapter.config import DateWindow, RunConfig, VersionRef
from packages.tradegrad_adapter.final_config import FinalEvaluationConfig


def synthetic_final_result(config=None, **kwargs):
    if config is None:
        base = synthetic_result()
        source = RunConfig.model_validate(base.run.config)
        values = source.model_dump(mode="json")
        values["windows"]["holdout"] = {"start": "2024-03-01", "end": "2024-03-12"}
        source = RunConfig.model_validate(values)
        config = FinalEvaluationConfig(
            selection_id=uuid4(),
            split_version_id=uuid4(),
            split_sha256="e" * 64,
            selected_strategy=VersionRef(id=str(uuid4()), sha256="f" * 64),
            source_config=source,
            source_config_sha256=source.sha256,
            holdout=DateWindow(start="2024-03-01", end="2024-03-12"),
            environment_digest=source.environment_digest,
            max_runtime_seconds=120,
        )
        kwargs.setdefault("dataset_manifest", base.dataset.manifest)
    result = synthetic_result(
        config.source_config,
        evaluation_window={
            "split": "holdout",
            **config.holdout.model_dump(mode="json"),
        },
        binding_config_sha256=config.sha256,
        binding_strategy=config.selected_strategy,
        **kwargs,
    )
    result.run.config = config.model_dump(mode="json")
    result.run.status = "succeeded"
    result.run.generation = result.evaluation.generation
    result.run.active_attempt_id = result.evaluation.attempt_id
    result.run.result = {"evaluation_id": str(result.evaluation.id)}
    result.dataset.market = config.source_config.market_profile.market
    result.dataset.start_date = config.source_config.windows.train.start
    result.dataset.end_date = config.holdout.end
    result.run.experiment_id = uuid4()
    result.run.final_selection_id = config.selection_id
    result.run.split_version_id = config.split_version_id
    result.evaluation.candidate_key = f"final:{config.selection_id}"
    return result


def synthetic_final_admission(result, snapshot):
    """Explicit DTO matching record_final_admission, not a persistence claim."""
    from backend.app.experiment_services import TRACKING_COVERAGE, dataset_identities

    run, dataset = result.run, result.dataset
    return SimpleNamespace(
        owner_id=run.owner_id,
        source_key=f"final:{run.id}:{run.generation}",
        run_id=run.id,
        experiment_id=run.experiment_id,
        split_version_id=run.split_version_id,
        dataset_version_id=run.dataset_version_id,
        start_date=dataset.start_date,
        end_date=date.fromisoformat(run.config["holdout"]["end"]),
        identities=dataset_identities(dataset),
        purpose="final_evaluation_admission",
        provenance={
            "generation": run.generation,
            "selection_id": str(run.final_selection_id),
            "final_config_sha256": run.request_sha256,
            "admission_before_dispatch": True,
            "requested_window": dict(run.config["holdout"]),
            "coverage": "conservative_dataset_prefix",
            "prior_exposure_snapshot": snapshot,
            "tracking_coverage": TRACKING_COVERAGE,
        },
    )
