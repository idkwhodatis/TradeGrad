"""Shared assertions executed only inside real native/portable final pipelines."""

import hashlib

from test_worker_recovery import require


def assert_final_workbench(
    h, client, other, item, selected, *, mode, content, result_view
):
    identifier = item["id"]
    candidate_id = selected["source_candidate_id"]
    strategy_id = selected["strategy_version_id"]
    exact = h.request(client, "GET", f"/evaluations/{identifier}/workbench").json()
    require(
        exact["run_kind"] == mode and exact["final_test_id"] == exact["run_id"],
        "Workbench final routed to a research run",
    )
    require(
        exact["evaluation_id"] == identifier
        and exact["run_id"] == item["run_id"]
        and exact["execution_attempt_id"] == item["attempt_id"],
        "Workbench final retargeted its immutable leaf",
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
    # Native and portable finals intentionally retain distinct result contracts.
    # Never fall back to another receipt or accept absent/unverified evidence.
    reference_key = {
        "native_final": "result_bundle",
        "portable_final": "portable_result",
    }[mode]
    reference = item["payload"].get(reference_key)
    require(isinstance(reference, dict), "Final mode-specific result receipt missing")
    result_sha256 = hashlib.sha256(content).hexdigest()
    require(
        reference.get("sha256") == result_sha256
        and reference.get("size_bytes") == len(content),
        "Final download differs from its immutable result receipt",
    )
    require(
        result_view["evaluation_id"] == identifier
        and result_view["run_id"] == item["run_id"]
        and result_view["selection_id"] == selected["id"]
        and result_view["artifact_sha256"] == result_sha256,
        "Final result projection differs from its immutable bytes",
    )
    require(
        exact["evidence_status"] == "registered"
        and exact["result_manifest_sha256"] == result_sha256,
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


def assert_replay_leaf(h, client, other, *, evaluation_id, bundle, result_sha256):
    """Actual isolated result evidence, all four immutable loader routes."""
    base = f"/evaluations/{evaluation_id}"
    manifest = h.request(client, "GET", base + "/trace-manifest").json()
    context = manifest["context"]
    require(
        context["evaluation_id"] == str(evaluation_id), "Replay selected another leaf"
    )
    require(
        context["result_manifest_sha256"] == result_sha256, "Replay result pin drifted"
    )
    pins = {
        key: context[key]
        for key in (
            "execution_attempt_id",
            "result_manifest_sha256",
            "trace_manifest_sha256",
        )
        if context[key] is not None
    }
    instruments = h.request(
        client, "GET", base + "/replay-instruments", params=pins
    ).json()
    require(instruments["context"] == context, "Replay instruments retargeted evidence")
    require(instruments["items"], "Frozen listing mapping is unavailable")
    require(
        instruments["market"]["membership_status"] == "available",
        "Configured universe was not read from frozen evidence",
    )
    h.request(other, "GET", base + "/replay-instruments", params=pins, status=404)
    h.request(
        client,
        "GET",
        base + "/replay-instruments",
        params={**pins, "result_manifest_sha256": "0" * 64},
        status=409,
    )
    portable = bundle.get("evaluation_mode") == "portable"
    orders = bundle["orders"] if portable else bundle["orders"]["rows"]
    # Generic native sources emit only actual order execution-result events, not
    # invented market callbacks. Portable completion records retain explicit IDs.
    events = h.request(
        client,
        "GET",
        base + "/decisions",
        params={
            **pins,
            "stage_id": "portable.native_execution" if portable else "execution_result",
            "limit": 1,
        },
    ).json()
    if not orders:
        require(
            not events["items"],
            "Order-free result unexpectedly gained linked execution events",
        )
        return
    require(
        events["items"], "Real engine orders lost their saved execution associations"
    )
    decision = events["items"][0]["decision_id"]
    execution_path = base + f"/decisions/{decision}/execution"
    execution = h.request(
        client, "GET", execution_path, params={**pins, "limit": 500}
    ).json()
    require(
        execution["context"] == context and execution["items"],
        "Explicit execution rows are absent",
    )
    for row in execution["items"]:
        if row["status"] != "available":
            require(
                row["record"] is None and row["missing_reason"],
                "Missing economic record became fabricated empty evidence",
            )
            continue
        section = {
            "logical_order": "logical_orders",
            "child_order": "orders",
            "fill": "fills",
        }[row["record_kind"]]
        if portable:
            original = bundle[section][row["record_index"]]
        else:
            table = bundle[section]
            original = dict(
                zip(
                    [column["name"] for column in table["columns"]],
                    table["rows"][row["record_index"]],
                    strict=True,
                )
            )
        require(
            row["record"] == original,
            "Execution projection changed actual ResultBundle economics",
        )
        if row["record_kind"] == "fill":
            require(
                str(original["order_id"]) in row["link"]["child_order_ids"],
                "Fill lacked explicit child-order association",
            )
    instrument = execution["items"][0]["instrument_id"]
    market_path = base + f"/decisions/{decision}/market"
    market = h.request(
        client,
        "GET",
        market_path,
        params={**pins, "instrument_id": instrument, "limit": 500},
    ).json()
    require(
        market["context"] == context and market["status"] == "available",
        "Frozen market replay is unavailable for a real linked instrument",
    )
    require(
        market["bars"]
        and market["market"]["dataset_sha256"]
        == instruments["market"]["dataset_sha256"],
        "Market replay lost pinned dataset provenance",
    )
    for path in (market_path, execution_path):
        h.request(
            other, "GET", path, params={**pins, "instrument_id": instrument}, status=404
        )
        h.request(
            client,
            "GET",
            path,
            params={
                **pins,
                "instrument_id": instrument,
                "trace_manifest_sha256": "0" * 64,
            },
            status=409,
        )
