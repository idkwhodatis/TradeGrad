"""One admission gate for typed, generic and package final evidence reads."""

from sqlalchemy import select

from backend.app.experiment_schemas import ExposureSnapshot
from backend.app.experiment_services import TRACKING_COVERAGE, _identities
from backend.app.models import DataUsageExposure
from backend.app.research_services import bounded_json
from backend.app.services import DomainError
from packages.tradegrad_adapter.config import DateWindow
from packages.tradegrad_adapter.final_config import FinalEvaluationConfig
from packages.tradegrad_adapter.portable_final_config import (
    PortableFinalEvaluationConfig,
)


def require_final_result_admission(session, run, evaluation, owner_id, *, dataset):
    """Validate the writer's exact recorded binding, before opening result files.

    ``dataset`` comes from the loader's validated frozen-selection binding. This
    is fail-closed corruption/repair-state detection, not database attestation or
    an independent proof of when the historical admission was recorded.
    """
    if run.owner_id != owner_id or evaluation.run_id != run.id:
        raise DomainError("not_found", "Final evaluation not found", 404)
    source_key = f"final:{run.id}:{evaluation.generation}"
    admission = session.scalar(
        select(DataUsageExposure).where(
            DataUsageExposure.owner_id == owner_id,
            DataUsageExposure.source_key == source_key,
        )
    )
    try:
        model = {
            "tradegrad.final-evaluation.v1": FinalEvaluationConfig,
            "tradegrad.portable.final-evaluation.v1": PortableFinalEvaluationConfig,
        }[run.config["schema_version"]]
        config = model.model_validate(run.config)
        prefix = DateWindow(start=dataset.start_date, end=config.holdout.end)
        if (
            admission is None
            or admission.owner_id != owner_id
            or admission.source_key != source_key
            or admission.run_id != run.id
            or admission.experiment_id != run.experiment_id
            or admission.split_version_id != run.split_version_id
            or run.split_version_id != config.split_version_id
            or run.final_selection_id != config.selection_id
            or evaluation.generation != run.generation
            or run.request_sha256 != config.sha256
            or admission.dataset_version_id != run.dataset_version_id
            or dataset.id != run.dataset_version_id
            or dataset.owner_id != owner_id
            or str(dataset.id) != config.source_config.dataset.id
            or dataset.content_sha256 != config.source_config.dataset.sha256
            or admission.start_date != prefix.start
            or admission.end_date != prefix.end
            or admission.identities != _identities(session, dataset)
            or admission.purpose != "final_evaluation_admission"
            or not isinstance(admission.provenance, dict)
        ):
            raise ValueError("Missing exact final admission")
        provenance = admission.provenance
        if (
            type(provenance.get("generation")) is not int
            or provenance["generation"] != run.generation
            or provenance.get("selection_id") != str(config.selection_id)
            or provenance.get("final_config_sha256") != config.sha256
            or provenance.get("requested_window")
            != config.holdout.model_dump(mode="json")
            or provenance.get("admission_before_dispatch") is not True
            or provenance.get("coverage") != "conservative_dataset_prefix"
            or provenance.get("tracking_coverage") != TRACKING_COVERAGE
        ):
            raise ValueError("Final admission provenance differs")
        snapshot = provenance["prior_exposure_snapshot"]
        bounded_json(snapshot, 256_000)
        return ExposureSnapshot.model_validate(snapshot).model_dump(mode="json")
    except (AttributeError, KeyError, TypeError, ValueError, DomainError):
        raise DomainError(
            "admission_required",
            "Final result has no exact generation admission evidence",
            409,
        ) from None
