"""Inert source/manifest final-selection checks; no policy, broker or engine runs."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.app.experiment_services import portable_selected_metadata
from backend.app.services import DomainError
from packages.strategy_sdk import (
    StateCodec,
    StateField,
    StateSchema,
    canonical_json,
    hash_json,
    parse_manifest,
)
from packages.tradegrad_adapter.portable_config import PortableRunConfig
from packages.tradegrad_adapter.portable_prompts import template_source

ROOT = Path(__file__).parents[2]
INERT = "raise AssertionError('SOURCE MUST NEVER EXECUTE')\nclass Policy: pass\n"
EDITABLE = (
    "raise AssertionError('SOURCE MUST NEVER EXECUTE')\n"
    "class Policy:\n"
    "    def decide(self, observation):\n"
    "        # ### EDIT START\n"
    "        return 1\n"
    "        # ### EDIT END\n"
)


def selection_inputs(source=INERT, *, authored=True, iterations=0):
    """Build only frozen data declarations and text; never load candidate Python."""
    raw = json.loads(
        (ROOT / "tests/tradegrad/fixtures/sealed-portable-config.json").read_bytes()
    )
    raw["optimizer"]["max_iterations"] = iterations
    config = PortableRunConfig.model_validate(raw)
    schema = StateSchema(
        name="momentum",
        version=1,
        fields=(
            StateField(name="last_session", kind="string"),
            StateField(name="decisions", kind="integer"),
        ),
    )
    state = StateCodec(schema).encode({"last_session": "", "decisions": 0})
    parameters = {"top_n": 1}
    manifest = {
        "strategy_kind": "portfolio",
        "strategy_id": config.seed_strategy.id,
        "code_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "parameters_sha256": hash_json(parameters),
        "market": "CN",
        "currency": "CNY",
        "calendar": "XSHG",
        "timezone": "Asia/Shanghai",
        "data": {"lookback_bars": 2, "warmup_bars": 2, "max_listings": 1},
        "universe": {
            "semantics": "user_fixed",
            "snapshot_sha256": config.universe.sha256,
            "survivorship_bias": "possible",
        },
        "prices": {"actions_snapshot_sha256": hash_json([])},
        "state_schema": schema.model_dump(mode="json"),
        "engine_profile": {
            "profile_sha256": config.engine_profile.sha256,
            "environment_sha256": config.environment_digest,
        },
    }
    if authored:
        manifest["authoring"] = {
            "parameter_schema": {
                "name": "parameters",
                "version": 1,
                "fields": [{"name": "top_n", "kind": "integer", "required": True}],
            },
            "initial_state": state.model_dump(mode="json"),
        }
    manifest = parse_manifest(canonical_json(manifest)).model_dump(mode="json")
    raw["seed_strategy"]["sha256"] = manifest["code_sha256"]
    raw["seed_manifest"]["sha256"] = hash_json(manifest)
    raw["seed_parameters"]["sha256"] = hash_json(parameters)
    raw["initial_state"] = state.model_dump(mode="json")
    config = PortableRunConfig.model_validate(raw)
    strategy = SimpleNamespace(
        id=UUID(config.seed_strategy.id),
        mode="portable",
        code=source,
        content_sha256=manifest["code_sha256"],
        manifest=manifest,
        parameters=parameters,
    )
    return config, strategy


def candidate(source):
    return SimpleNamespace(
        code=source, code_sha256=hashlib.sha256(source.encode()).hexdigest()
    )


@pytest.mark.parametrize(
    "source",
    [INERT, "# Authored outside reference markers\n" + template_source("portfolio")],
)
def test_authored_zero_iteration_selection_preserves_full_source_contract(source):
    config, strategy = selection_inputs(source)
    original = copy.deepcopy(strategy.manifest)
    manifest, parameters = portable_selected_metadata(
        config, strategy, candidate(source)
    )
    assert manifest == original
    assert manifest["authoring"]["source_kind"] == "user_authored"
    assert strategy.manifest == original and strategy.code == source
    assert parameters == strategy.parameters and parameters is not strategy.parameters


@pytest.mark.parametrize("iterations", [0, 1])
def test_authored_revisions_preserve_their_own_scaffolding(iterations):
    config, strategy = selection_inputs(EDITABLE, iterations=iterations)
    selected = candidate(EDITABLE.replace("return 1", "return 2"))
    manifest, parameters = portable_selected_metadata(config, strategy, selected)
    expected = copy.deepcopy(strategy.manifest)
    expected["code_sha256"] = selected.code_sha256
    assert manifest == expected and parameters == strategy.parameters
    assert strategy.code == EDITABLE


@pytest.mark.parametrize("iterations", [0, 1])
def test_authored_revisions_cannot_change_protected_source(iterations):
    config, strategy = selection_inputs(EDITABLE, iterations=iterations)
    with pytest.raises(DomainError, match="Stored portable policy"):
        portable_selected_metadata(
            config, strategy, candidate(EDITABLE + "\n# Changed protected source\n")
        )


@pytest.mark.parametrize("iterations", [0, 1])
def test_missing_edit_region_cannot_admit_authored_revision_or_optimization(iterations):
    config, strategy = selection_inputs(iterations=iterations)
    selected = candidate(INERT + "# Revision\n" if iterations == 0 else INERT)
    with pytest.raises(DomainError, match="Stored portable policy"):
        portable_selected_metadata(config, strategy, selected)


def test_reference_selection_still_requires_the_exact_reference_scaffolding():
    source = template_source("portfolio")
    config, strategy = selection_inputs(source, authored=False)
    selected = candidate(source.replace("scores = []", "scores = list()"))
    manifest, _ = portable_selected_metadata(config, strategy, selected)
    assert "authoring" not in manifest
    assert manifest["code_sha256"] == selected.code_sha256
    with pytest.raises(DomainError, match="Stored portable policy"):
        portable_selected_metadata(config, strategy, candidate("# Custom\n" + source))


@pytest.mark.parametrize(
    "mutation", ["source", "source_hash", "manifest", "parameters"]
)
def test_authored_selection_keeps_immutable_binding_checks(mutation):
    config, strategy = selection_inputs()
    if mutation == "source":
        strategy.code += "# Changed stored bytes\n"
    elif mutation == "source_hash":
        strategy.content_sha256 = "0" * 64
    elif mutation == "manifest":
        strategy.manifest["data"]["max_listings"] = 2
    else:
        strategy.parameters["top_n"] = 2
    with pytest.raises(DomainError, match="Stored portable policy"):
        portable_selected_metadata(config, strategy, candidate(INERT))
