"""Explicit synthetic persisted-v1 evidence, never legacy engine execution.

This fixture intentionally preserves the historical false-absence declaration.
It is for compatibility/restore checks, not evidence that current semantics are
valid or permission to relabel a current computation as an old algorithm.
"""

from packages.research_trace.comparison import (
    comparison_input_hash,
    comparison_side_pins,
)
from packages.research_trace.comparison_contracts import (
    DEFAULT_LIMITS,
    FIELD_TOLERANCES,
    LEGACY_ALGORITHM_VERSION,
    SECTIONS,
    ComparisonManifest,
    ComparisonRecord,
    ComparisonSectionSummary,
)
from packages.research_trace.comparison_storage import (
    ComparisonControl,
    ComparisonWriter,
)


def write_synthetic_legacy_result(left, right, path, publication, *, durable_root=None):
    control = ComparisonControl()
    with control.connection() as connection:
        writer = ComparisonWriter(
            path, publication, control, connection, durable_root=durable_root
        )
        writer.append(
            ComparisonRecord(
                row_id="historical-false-absence",
                section="events",
                event_pair_id="historical-unpaired-event",
                status="left_only",
                left_state="observed",
                right_state="observed_absence",
                left={"historical_fixture": True},
                right=None,
                match_basis="none",
                missing_reason="Synthetic saved v1 unmatched-event declaration",
            )
        )
        writer.finish_blocks()
        sections = {
            section: ComparisonSectionSummary(
                left_availability="not_collected",
                right_availability="not_collected",
                definition="Synthetic historical compatibility fixture",
            )
            for section in SECTIONS
        }
        sections["events"] = ComparisonSectionSummary(
            left_availability="available",
            right_availability="available",
            observed_equality="different",
            rows=1,
            left_only=1,
            definition="Synthetic historical false-absence declaration",
        )
        manifest = ComparisonManifest(
            publication=publication,
            algorithm_version=LEGACY_ALGORITHM_VERSION,
            input_hash=comparison_input_hash(
                left, right, algorithm_version=LEGACY_ALGORITHM_VERSION
            ),
            left=comparison_side_pins(left),
            right=comparison_side_pins(right),
            field_tolerances=FIELD_TOLERANCES,
            limits=DEFAULT_LIMITS,
            namespace_status="unknown",
            namespace_reason="Synthetic historical fixture",
            pairing_fields=[],
            code_changed=False,
            configuration_differences=[],
            mechanism_changes=[],
            sections=sections,
            observed_behavior="changed",
            observed_equal_sections=[],
            artifacts=writer.refs,
            warnings=["Synthetic historical evidence; never a current computation"],
        )
        return writer.publish_manifest(manifest)
