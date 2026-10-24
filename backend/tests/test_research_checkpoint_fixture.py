"""Real checkpoint bytes from the shared fixture; no database execution claim."""

from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.research_services import _load_checkpoint
from backend.app.services import DomainError
from backend.tests.research_view_fixtures import synthetic_result
from packages.tradegrad_adapter.checkpoint import CheckpointStore, json_digest
from packages.tradegrad_adapter.config import RunConfig
from tests.integration.test_research_lifecycle import publish_initial


@pytest.mark.parametrize("mode", ["off", "research"])
def test_checkpoint_fixture_uses_same_canonical_defaults_as_run_admission(
    tmp_path, mode
):
    payload = deepcopy(synthetic_result().run.config)
    code = "# Synthetic checkpoint fixture, never executed.\n"
    payload["seed_strategy"]["sha256"] = sha256(code.encode()).hexdigest()
    payload["research_capture"] = {"mode": mode}
    config = RunConfig.model_validate(payload)
    settings = SimpleNamespace(storage_root=tmp_path)
    attempt = SimpleNamespace(run_id=uuid4(), id=uuid4())
    # Preparing state does not use an engine/session. Only the publication closure
    # would enter the real-PG integration boundary; it is deliberately not called.
    _, state, store = publish_initial.__wrapped__(
        None,
        settings,
        SimpleNamespace(strategy=SimpleNamespace(code=code)),
        payload,
        attempt,
    )
    assert state["inputs"]["config"] == config.model_dump(mode="json")
    assert state["inputs"]["config"]["research_capture"] != {"mode": mode}
    ref = store.commit(state)
    _, _, admitted, _ = _load_checkpoint(
        ref, config, settings, run_id=attempt.run_id, attempt_id=attempt.id
    )
    assert admitted == state
    # The fixture correction does not relax the production exact-config guard.
    rejected = deepcopy(state)
    rejected["inputs"]["config"] = payload
    invalid_store = CheckpointStore(store.root, json_digest(rejected["inputs"]))
    invalid = invalid_store.commit(rejected)
    with pytest.raises(DomainError, match="config differs from immutable RunConfig"):
        _load_checkpoint(
            invalid, config, settings, run_id=attempt.run_id, attempt_id=attempt.id
        )
