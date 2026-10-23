"""Real final loader/admission/file checks with explicit fake persistence only."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_final_loader_admission as admission

from backend.app.services import DomainError

harness = admission.harness


def endpoint(h, suffix, owner=None):
    prefix = (
        "portable_final"
        if h.result.bundle.get("evaluation_mode") == "portable"
        else "final"
    )
    method = getattr(h.routes, f"read_{prefix}_{suffix}")
    h.session.commit = lambda: None
    kwargs = {
        "evaluation_id": h.result.evaluation.id,
        "user": SimpleNamespace(id=owner or h.result.run.owner_id),
        "session": h.session,
        "request": SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(settings=h.settings))
        ),
    }
    if suffix == "price_replay":
        kwargs["listing_id"] = uuid4()
    return method(**kwargs)


def test_mode_analysis_uses_actual_admission_and_keeps_frozen_selection(harness):
    h = harness
    view = endpoint(h, "return_analysis")
    assert view.context.split == "holdout"
    assert view.context.selection_id == h.selection.id
    assert view.context.artifact_sha256 == h.result.artifact.sha256
    assert len(h.exposures) == 1


@pytest.mark.parametrize("suffix", ["return_analysis", "price_replay"])
@pytest.mark.parametrize("failure", ["unadmitted", "foreign", "checksum"])
def test_new_mode_routes_cannot_bypass_final_fences(harness, suffix, failure):
    h = harness
    if failure == "unadmitted":
        h.session.admission_row = None
    if failure == "checksum":
        h.path.write_bytes(h.result.content + b" ")
    with pytest.raises(DomainError) as error:
        endpoint(h, suffix, uuid4() if failure == "foreign" else None)
    assert (
        error.value.code
        == {
            "unadmitted": "admission_required",
            "foreign": "not_found",
            "checksum": "artifact_integrity",
        }[failure]
    )
    assert not h.exposures
