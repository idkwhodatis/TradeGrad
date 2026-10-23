"""Assertions called only by the explicitly opted-in real process pipelines."""

import hashlib


def assert_mode_exploration(
    h, client, other, *, mode, evaluation_id, content, bundle, selection_id=None
):
    prefixes = {
        "native_final": "/final-evaluations/evaluations",
        "portable_research": "/portable-evaluations",
        "portable_final": "/portable-final-evaluations/evaluations",
    }
    path = f"{prefixes[mode]}/{evaluation_id}"
    analysis = h.request(client, "GET", path + "/return-analysis").json()
    context = analysis["context"]
    assert context["mode"] == mode and context["evaluation_id"] == str(evaluation_id)
    assert context["artifact_sha256"] == hashlib.sha256(content).hexdigest()
    assert context["selection_id"] == (str(selection_id) if selection_id else None)
    assert (context["split"] == "holdout") == mode.endswith("final")
    assert analysis["observations"] > 0 and context["listings"]
    listing = context["listings"][0]["listing_id"]
    replay = h.request(
        client,
        "GET",
        path + "/price-replay",
        params={"listing_id": listing, "limit": 500},
    ).json()
    assert replay["context"] == context and replay["bars"]
    assert replay["dataset_sha256"] == context["dataset_sha256"]
    if mode.startswith("portable"):
        assert context["ledger_origin"] == "trusted_engine"
        assert (
            replay["indicators_status"] == "not_persisted" and not replay["indicators"]
        )
        for marker in replay["markers"]:
            original = bundle["fills"][marker["row_index"]]
            assert marker["timestamp"] == original["native_at"]
            assert marker["native_timestamp_ns"] == original["native_timestamp_ns"]
            assert (
                marker["economic_reference_time"] == original["economic_reference_time"]
            )
            assert marker["price"] == original["price"]
            assert marker["fill_id"] == original["fill_id"]
    for suffix in ("/return-analysis", f"/price-replay?listing_id={listing}"):
        h.request(other, "GET", path + suffix, status=404)
    # Reading/sorting projections never rewrites the authoritative result bytes.
    download = "/result/download" if mode.endswith("final") else "/results/download"
    assert h.request(client, "GET", path + download).content == content
