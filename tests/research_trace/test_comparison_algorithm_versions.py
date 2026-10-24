"""Immutable algorithm identity across real Parquet readers and restored bytes."""

from uuid import uuid4

import pytest

from packages.research_trace.comparison import ComparisonReader, comparison_input_hash
from packages.research_trace.comparison_contracts import (
    ALGORITHM_VERSION,
    LEGACY_ALGORITHM_VERSION,
    ComparisonError,
    ComparisonManifest,
    ComparisonPublication,
    EventNamespace,
)
from packages.research_trace.contracts import canonical_bytes, digest
from tests.research_trace.comparison_version_fixtures import (
    write_synthetic_legacy_result,
)
from tests.research_trace.test_comparison import make_input, run_compare


@pytest.fixture
def inputs(tmp_path):
    namespace = EventNamespace(
        dataset_version_id=uuid4(),
        dataset_sha256="a" * 64,
        universe_version_id=None,
        universe_sha256=None,
        frozen_universe_sha256="b" * 64,
        instrument_namespace="native_symbol",
    )
    return (
        make_input(tmp_path / "left", namespace, event_ids=("frozen:left",)),
        make_input(tmp_path / "right", namespace, event_ids=("native_order:right",)),
    )


def test_saved_false_absence_remains_immutable_but_new_computation_is_distinct(
    tmp_path, inputs
):
    old, artifacts = write_synthetic_legacy_result(
        *inputs,
        tmp_path / "legacy",
        ComparisonPublication(
            comparison_id=uuid4(), run_id=uuid4(), execution_attempt_id=uuid4()
        ),
    )
    saved = {item.name: item.path.read_bytes() for item in artifacts}
    paths = {item.name: item.path for item in artifacts}
    raw = canonical_bytes(old)
    loaded = ComparisonManifest.model_validate_json(raw)
    historical = ComparisonReader(loaded, paths).page("events").items
    assert historical[0].right_state == "observed_absence"
    assert loaded.algorithm_version == LEGACY_ALGORITHM_VERSION
    assert canonical_bytes(loaded) == raw and digest(loaded) == digest(old)

    current, reader, _ = run_compare(tmp_path / "current", *inputs)
    assert current.algorithm_version == ALGORITHM_VERSION
    assert current.input_hash != old.input_hash
    assert current.input_hash == comparison_input_hash(*inputs)
    assert all(
        "observed_absence" not in {row.left_state, row.right_state}
        for row in reader.page("events").items
    )
    # Repeated historical reads, including after new computation, retain all
    # original semantics and byte identities rather than silently upgrading.
    assert ComparisonReader(loaded, paths).page("events").items == historical
    assert {item.name: item.path.read_bytes() for item in artifacts} == saved
    # Restore the exact archived bytes at another storage root; immutable IDs
    # and all public fields remain unchanged without migration or computation.
    restored = tmp_path / "restored"
    restored.mkdir()
    restored_paths = {}
    for name, content in saved.items():
        restored_paths[name] = restored / name
        restored_paths[name].write_bytes(content)
    restored_manifest = ComparisonManifest.model_validate_json(raw)
    assert (
        ComparisonReader(restored_manifest, restored_paths).page("events").items
        == historical
    )
    assert canonical_bytes(restored_manifest) == raw


@pytest.mark.parametrize(
    "change",
    [
        {"algorithm_version": LEGACY_ALGORITHM_VERSION},
        {"algorithm_version": "tradegrad.observed-comparison.v999"},
        {"input_hash": "0" * 64},
        {"tolerance_version": "tradegrad.comparison-tolerances.v999"},
        {"field_tolerances": {}},
    ],
)
def test_reader_rejects_mismatched_or_unsupported_declarations(
    tmp_path, inputs, change
):
    manifest, reader, _ = run_compare(tmp_path / "current", *inputs)
    with pytest.raises(ComparisonError):
        ComparisonReader(manifest.model_copy(update=change), reader.paths)


@pytest.mark.parametrize(
    "algorithm_version", [LEGACY_ALGORITHM_VERSION, ALGORITHM_VERSION]
)
@pytest.mark.parametrize("mode", ["legacy", "mixed"])
def test_both_algorithms_read_legacy_and_mixed_physical_rows_without_rewriting(
    tmp_path, algorithm_version, mode
):
    # The payload-codec compatibility gate uses exact observed equal pairs; its
    # synthetic manifest identity can be declared independently of row encoding.
    import tests.research_trace.test_comparison_codec as codecs

    original = codecs._fixture

    def fixture(path):
        manifest, reader, artifacts = original(path)
        changed = manifest.model_copy(update={"algorithm_version": algorithm_version})
        changed = changed.model_copy(
            update={
                "input_hash": digest(
                    {
                        "left": changed.left.model_dump(mode="json"),
                        "right": changed.right.model_dump(mode="json"),
                        "algorithm": algorithm_version,
                        "tolerance_version": changed.tolerance_version,
                        "tolerances": changed.field_tolerances,
                        "limits": changed.limits.model_dump(mode="json"),
                    }
                )
            }
        )
        return changed, ComparisonReader(changed, reader.paths), artifacts

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(codecs, "_fixture", fixture)
        codecs.test_legacy_and_mixed_blocks_preserve_all_public_queries(tmp_path, mode)
