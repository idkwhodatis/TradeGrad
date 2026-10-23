"""Pure synthetic authored-result identities, never candidate or engine execution."""

import pytest

from backend.app.final_evaluation_views import validate_final_persisted_result
from backend.app.research_views import validate_persisted_result
from backend.app.services import DomainError
from backend.tests.final_result_fixtures import synthetic_final_result
from backend.tests.research_view_fixtures import synthetic_result
from packages.tradegrad_adapter.config import RunConfig
from packages.tradegrad_adapter.final_config import FinalEvaluationConfig


def declared_source(config, allocation):
    values = config.model_dump(mode="json")
    values["strategy_contract"] = (
        {
            "lookback_bars": 20,
            "warmup_bars": 20,
            "requires_shorting": False,
            "parameters": [
                {
                    "name": "allocation",
                    "kind": "number",
                    "value": allocation,
                    "minimum": 0.1,
                    "maximum": 0.9,
                }
            ],
        }
        if allocation is not None
        else None
    )
    return RunConfig.model_validate(values)


@pytest.mark.parametrize("final", [False, True])
@pytest.mark.parametrize("executed_allocation", [None, 0.2, 0.8])
def test_authored_native_result_binds_the_executed_declaration(
    final, executed_allocation
):
    base = synthetic_final_result() if final else synthetic_result()
    if final:
        original = FinalEvaluationConfig.model_validate(base.run.config)
        source = original.source_config
    else:
        source = RunConfig.model_validate(base.run.config)
    intended = declared_source(source, 0.2)
    executed = declared_source(source, executed_allocation)
    if final:
        values = original.model_dump(mode="json")
        values.update(
            source_config=intended.model_dump(mode="json"),
            source_config_sha256=intended.sha256,
        )
        intended_config = FinalEvaluationConfig.model_validate(values)
        result = synthetic_result(
            executed,
            dataset_manifest=base.dataset.manifest,
            evaluation_window=base.evaluation.window,
            binding_config_sha256=intended_config.sha256,
            binding_strategy=intended_config.selected_strategy,
        )
        result.run.status = "succeeded"
        result.run.generation = result.evaluation.generation
        result.run.active_attempt_id = result.evaluation.attempt_id
        result.run.result = {"evaluation_id": str(result.evaluation.id)}
        result.run.final_selection_id = intended_config.selection_id
        result.run.split_version_id = intended_config.split_version_id
        validate = validate_final_persisted_result
    else:
        intended_config = intended
        result = synthetic_result(
            executed,
            dataset_manifest=base.dataset.manifest,
            binding_config_sha256=intended.sha256,
        )
        validate = validate_persisted_result
    # Even correctly hashed artifacts/payloads claiming the intended run hash must
    # bind the actual request's exact declaration, including all parameter values.
    result.run.config = intended_config.model_dump(mode="json")
    args = (
        result.evaluation,
        result.run,
        result.dataset,
        result.artifact,
        result.content,
    )
    if executed_allocation == 0.2:
        assert validate(*args) == result.bundle
    else:
        with pytest.raises(DomainError) as error:
            validate(*args)
        assert error.value.code == "result_integrity"
