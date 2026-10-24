"""Inert acceptance-helper regressions, not real API/PG/container acceptance.

Exercise the production receipt projection with both final modes. The real
pipelines separately provide downloaded, validated native/portable result bytes.
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from workbench_assertions import assert_final_workbench

from backend.app.final_summary_access import diagnostic_payload


def test_inert_final_contract_regressions_are_mandatory_in_hosted_supervisor():
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/v1-validation.yml"
    ).read_text()
    command = next(
        line for line in workflow.splitlines() if "pytest backend/tests" in line
    )
    assert "tests/system/test_final_workbench_contract.py" in command.split()


@pytest.fixture(params=["native_final", "portable_final"])
def final_contract(request):
    mode = request.param
    reference_key = "portable_result" if mode == "portable_final" else "result_bundle"
    content = b'{"inert_acceptance_helper_fixture":true}'
    sha256 = hashlib.sha256(content).hexdigest()
    identifier, run_id, attempt_id, candidate_id, strategy_id, selection_id = (
        str(uuid4()) for _ in range(6)
    )
    payload = {
        "status": "succeeded",
        "exit_code": 0,
        reference_key: {
            "artifact_id": str(uuid4()),
            "sha256": sha256,
            "size_bytes": len(content),
        },
    }
    item = {
        "id": identifier,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "code_sha256": "a" * 64,
        "payload": diagnostic_payload(SimpleNamespace(payload=payload)),
    }
    selected = {
        "id": selection_id,
        "source_candidate_id": candidate_id,
        "strategy_version_id": strategy_id,
    }
    exact = {
        "run_kind": mode,
        "evaluation_id": identifier,
        "run_id": run_id,
        "execution_attempt_id": attempt_id,
        "final_test_id": run_id,
        "source_association": {
            "candidate_id": candidate_id,
            "strategy_version_id": strategy_id,
            "strategy_basis": "selected_final",
        },
        "candidate_id": candidate_id,
        "strategy_version_id": strategy_id,
        "code_sha256": item["code_sha256"],
        "status": "succeeded",
        "split": "holdout",
        "evidence_status": "registered",
        "result_manifest_sha256": sha256,
    }
    result_view = {
        "evaluation_id": identifier,
        "run_id": run_id,
        "selection_id": selection_id,
        "artifact_sha256": sha256,
    }
    workbench_path = f"/evaluations/{identifier}/workbench"
    list_paths = {
        f"/candidates/{candidate_id}/evaluations",
        f"/strategy-versions/{strategy_id}/evaluations",
    }
    denied = []

    def request(client, method, path, *, status=200, params=None):
        assert method == "GET"
        if client == "other":
            assert status == 404 and path in list_paths | {workbench_path}
            denied.append(path)
            return SimpleNamespace(json=dict)
        assert client == "owner" and status == 200
        if path == workbench_path:
            data = exact
        elif path in list_paths:
            assert params == {"limit": 100}
            data = {"data": [{"evaluation_id": identifier}]}
        else:
            assert path == f"/candidates/{candidate_id}/lineage"
            data = {"focus": {"final_selection_ids": [selection_id]}}
        return SimpleNamespace(json=lambda: data)

    h = SimpleNamespace(request=request)

    def check():
        assert_final_workbench(
            h,
            "owner",
            "other",
            item,
            selected,
            mode=mode,
            content=content,
            result_view=result_view,
        )

    return SimpleNamespace(
        check=check,
        item=item,
        exact=exact,
        result_view=result_view,
        reference_key=reference_key,
        denied=denied,
        expected_denied=list_paths | {workbench_path},
    )


def test_final_mode_receipt_matches_bytes_projection_and_workbench(final_contract):
    f = final_contract
    f.check()
    assert set(f.denied) == f.expected_denied and len(f.denied) == 3


@pytest.mark.parametrize("replace_with_other_mode", [False, True])
def test_missing_mode_specific_receipt_never_uses_another_contract(
    final_contract, replace_with_other_mode
):
    f = final_contract
    reference = f.item["payload"].pop(f.reference_key)
    if replace_with_other_mode:
        other_key = (
            "result_bundle"
            if f.reference_key == "portable_result"
            else "portable_result"
        )
        f.item["payload"][other_key] = reference
    with pytest.raises(pytest.fail.Exception, match="mode-specific result receipt"):
        f.check()


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("size_bytes", 1)])
def test_receipt_must_bind_exact_downloaded_bytes(final_contract, field, value):
    f = final_contract
    f.item["payload"][f.reference_key][field] = value
    # Matching workbench metadata alone cannot substitute for verified bytes.
    if field == "sha256":
        f.exact["result_manifest_sha256"] = value
        f.result_view["artifact_sha256"] = value
    with pytest.raises(pytest.fail.Exception, match="immutable result receipt"):
        f.check()


@pytest.mark.parametrize(
    "field,value",
    [
        ("evaluation_id", "another-evaluation"),
        ("run_id", "another-run"),
        ("selection_id", "another-selection"),
        ("artifact_sha256", "0" * 64),
    ],
)
def test_result_projection_cannot_retarget_or_repin_the_final(
    final_contract, field, value
):
    f = final_contract
    f.result_view[field] = value
    with pytest.raises(pytest.fail.Exception, match="projection differs"):
        f.check()


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("run_kind", "native_research", "routed to a research run"),
        ("evaluation_id", "another-evaluation", "immutable leaf"),
        ("execution_attempt_id", "another-attempt", "immutable leaf"),
        ("result_manifest_sha256", "0" * 64, "result pin differs"),
        ("evidence_status", "unavailable", "result pin differs"),
    ],
)
def test_workbench_cannot_retarget_or_drop_the_final_pin(
    final_contract, field, value, message
):
    f = final_contract
    f.exact[field] = value
    with pytest.raises(pytest.fail.Exception, match=message):
        f.check()


def test_workbench_cannot_switch_to_the_other_final_mode(final_contract):
    f = final_contract
    f.exact["run_kind"] = (
        "native_final" if f.exact["run_kind"] == "portable_final" else "portable_final"
    )
    with pytest.raises(pytest.fail.Exception, match="routed to a research run"):
        f.check()
