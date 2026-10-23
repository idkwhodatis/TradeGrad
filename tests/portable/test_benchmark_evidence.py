"""Pure artifact publication checks with inert accounting fakes, no policy/SDK calls."""

import copy
import hashlib
import json
from uuid import UUID

import pytest
from test_persisted_evidence import IMAGES, evidence

from packages.tradegrad_adapter.benchmarks import validate_comparison
from packages.tradegrad_adapter.config import BenchmarkConfig
from packages.tradegrad_adapter.portable_results import canonical_json_bytes
from workers.research.portable_optimization import publish_portable_artifacts


def benchmark_evidence():
    bundle, config, row = evidence()
    reference = BenchmarkConfig(
        dataset={"id": str(UUID(int=901)), "sha256": "c" * 64},
        listing_id=str(UUID(int=902)),
    )
    config = config.model_copy(update={"benchmark": reference})
    bundle["identity"]["run_config_sha256"] = config.sha256
    prepared = {
        "reference": reference.model_dump(mode="json"),
        "source_file": "data/BENCH.csv",
        "source_file_sha256": "d" * 64,
        "source_as_of": "2025-02-01T00:00:00Z",
        "source_classification": "synthetic",
        "currency": "USD",
        "timezone": "America/New_York",
        "calendar": "XNYS",
        "calendar_version": "synthetic-test.v1",
        "price_semantics": {
            "native_mode": "raw",
            "split_basis": "unadjusted",
            "dividend_basis": "unadjusted",
        },
        "window": {k: row.window[k] for k in ("start", "end")},
        "observations": [
            [item["native_at"][:10], 100 + i * 10]
            for i, item in enumerate(bundle["account_series"])
        ],
    }
    return bundle, config, row, prepared


@pytest.mark.parametrize("failure", [None, "missing", "foreign_ref", "gap"])
def test_selected_portable_publication_binds_derived_artifact_not_raw_runner(
    tmp_path, failure
):
    bundle, config, row, prepared = benchmark_evidence()
    raw = canonical_json_bytes(bundle)
    original = copy.deepcopy(bundle)
    output = tmp_path / "output"
    output.mkdir()
    (output / "portable-result.json").write_bytes(raw)
    if failure == "missing":
        prepared = None
    elif failure == "foreign_ref":
        prepared["reference"]["dataset"]["sha256"] = "f" * 64
    elif failure == "gap":
        prepared["observations"].pop(1)
    kwargs = {
        "storage_root": tmp_path,
        "artifact_root": tmp_path / "artifacts",
        "output_root": output,
        "result": {
            "status": "succeeded",
            "bundle": bundle,
            "image_digests": IMAGES,
            "result_trust": bundle["result_trust"],
            "ledger_origin": "trusted_engine",
            "runtime_certified": False,
            "logs": {},
        },
        "config": config,
        "code_sha256": row.code_sha256,
        "window": row.window,
        "images": IMAGES,
        "max_bytes": 8 * 1_048_576,
        "benchmark_input": prepared,
    }
    if failure:
        with pytest.raises(ValueError):
            publish_portable_artifacts(**kwargs)
        assert not (tmp_path / "artifacts" / "portable-result.json").exists()
    else:
        records, enriched = publish_portable_artifacts(**kwargs)
        assert len(records) == 1
        durable = (tmp_path / records[0]["storage_key"]).read_bytes()
        assert hashlib.sha256(durable).hexdigest() == records[0]["sha256"]
        assert durable != raw and json.loads(durable) == enriched
        assert enriched["benchmark"]["input"] == prepared
        validate_comparison(enriched["benchmark"], enriched)
        assert enriched["benchmark"]["metrics"][1]["value"] == pytest.approx(20)
        assert enriched["benchmark"]["metrics"][2]["unit"] == "percentage_points"
    assert (output / "portable-result.json").read_bytes() == raw
    assert bundle == original and "benchmark" not in bundle
