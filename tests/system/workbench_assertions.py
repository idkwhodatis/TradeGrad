"""Shared assertions executed only inside real native/portable final pipelines."""

from test_worker_recovery import require


def assert_final_workbench(h, client, other, item, selected):
    identifier = item["id"]
    candidate_id = selected["source_candidate_id"]
    strategy_id = selected["strategy_version_id"]
    exact = h.request(client, "GET", f"/evaluations/{identifier}/workbench").json()
    require(
        exact["run_kind"] in {"native_final", "portable_final"}
        and exact["final_test_id"] == exact["run_id"],
        "Workbench final routed to a research run",
    )
    require(
        exact["source_association"]["candidate_id"] == candidate_id
        and exact["source_association"]["strategy_version_id"] == strategy_id
        and exact["source_association"]["strategy_basis"] == "selected_final",
        "Workbench final source association differs from immutable selection",
    )
    require(
        exact["candidate_id"] == candidate_id,
        "Workbench final lost its actual source candidate",
    )
    require(
        exact["strategy_version_id"] == strategy_id,
        "Workbench final retargeted its frozen strategy version",
    )
    require(
        exact["code_sha256"] == item["code_sha256"],
        "Workbench final source hash differs",
    )
    require(
        exact["status"] == "succeeded" and exact["split"] == "holdout",
        "Workbench final fabricated its leaf status",
    )
    require(
        exact["result_manifest_sha256"] == item["payload"]["result_bundle"]["sha256"],
        "Workbench final result pin differs",
    )
    for kind, resource_id in (
        ("candidates", candidate_id),
        ("strategy-versions", strategy_id),
    ):
        path = f"/{kind}/{resource_id}/evaluations"
        page = h.request(client, "GET", path, params={"limit": 100}).json()
        require(
            any(row["evaluation_id"] == identifier for row in page["data"]),
            "Workbench omitted cross-run selected final evaluation",
        )
        h.request(other, "GET", path, status=404)
    lineage = h.request(client, "GET", f"/candidates/{candidate_id}/lineage").json()
    require(
        selected["id"] in lineage["focus"]["final_selection_ids"],
        "Immutable final-selection marker missing",
    )
    h.request(other, "GET", f"/evaluations/{identifier}/workbench", status=404)
