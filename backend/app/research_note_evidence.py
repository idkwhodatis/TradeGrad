"""Re-admit originals on every note read. Never copy outcome facts into notes.

The only persisted server snapshots are immutable identity, physical bindings and
retention registrations. Current source/result loaders remain the authority.
"""

from dataclasses import dataclass, field
from time import monotonic
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import or_, select

from backend.app.comparison_schemas import ComparisonSideCreate
from backend.app.comparison_services import (
    comparison_config,
    comparison_section_view,
    load_comparison_side,
    load_owned_comparison_result,
    release_comparison_access,
)
from backend.app.models import (
    Artifact,
    ControlledArm,
    Evaluation,
    ExecutionAttempt,
    IndependentEvaluation,
    ResearchRun,
    StrategySpecRevision,
)
from backend.app.research_note_selection import href, reference_selection
from backend.app.research_services import payload_hash
from backend.app.result_access import record_verified_result_access
from backend.app.services import DomainError, advisory_lock
from backend.app.storage import safe_path
from backend.app.trace_services import _event, _query
from backend.app.workbench_services import _clause, _spec_public, owned_source
from packages.research_trace.contracts import canonical_bytes, digest
from packages.research_trace.specification import source_locations
from packages.research_trace.storage import (
    TraceStorageError,
    _verified_file,
    manifest_name,
)

MAX_PINS = 100000
PROTECTED_SCHEMAS = {
    "tradegrad.final-evaluation.v1",
    "tradegrad.portable.final-evaluation.v1",
    "tradegrad.independent-evaluation.v1",
    "tradegrad.portable.independent-evaluation.v1",
}


def mismatch(message="Evidence differs from its immutable note reference"):
    return DomainError("note_evidence_pin_mismatch", message, 409)


@dataclass
class ResolvedEvidence:
    snapshot: dict
    bindings: list = field(default_factory=list)
    pins: list = field(default_factory=list)
    href: str | None = None


class EvidenceResolver:
    def __init__(self, session, owner_id, settings, *, purpose="result_preview"):
        self.session, self.owner_id, self.settings = session, owner_id, settings
        self.purpose = purpose
        self.leaves = {}
        self.verified_files = set()
        self.total_verified_bytes = 0
        self.resolved = {}
        self.resolve_calls = 0
        self.admission_checked = set()
        self.deadline = monotonic() + 30
        self.loader_work = 0
        self.revision_read_bytes = 0
        advisory_lock(session, f"submit:{owner_id}")

    def check_budget(self, cost=0):
        self.loader_work += cost
        if monotonic() > self.deadline or self.loader_work > 128:
            raise DomainError(
                "note_evidence_capacity",
                "Note admission exceeds its 30-second / 128-layer work budget; narrow the requested page",
                422,
            )

    def check_leaf_budget(self, pin):
        # Estimate the recursive source-loader layers without reading bytes.
        # Independent loaders themselves recurse, and our inventory traversal
        # also visits ancestors, so debit the conservative triangular cost.
        current, seen = pin.evaluation_id, set()
        while current is not None:
            self.check_budget()
            if current in seen or len(seen) >= 16:
                raise DomainError(
                    "independent_source_depth",
                    "Evidence ancestry exceeds bounded admission",
                    409,
                )
            seen.add(current)
            pair = self.session.execute(
                select(Evaluation, ResearchRun)
                .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
                .where(Evaluation.id == current, ResearchRun.owner_id == self.owner_id)
            ).one_or_none()
            if pair is None:
                raise DomainError("not_found", "Evaluation not found", 404)
            _, run = pair
            raw = (
                run.config.get("source", {})
                if run.config.get("execution_mode") == "independent_evaluation"
                else {}
            )
            try:
                current = UUID(raw["evaluation_id"]) if raw else None
            except (TypeError, ValueError, KeyError):
                raise mismatch("Immutable source ancestry is malformed") from None
        self.check_budget(len(seen) * (len(seen) + 1) // 2)

    def admit_snapshot(self, snapshot):
        """Check every saved physical ancestor before fallible source/file reads.

        This separate metadata pass prevents an early missing left-side file from
        hiding a revoked final admission on the right side. It is also the gate
        for outcome-text search/count, independent of the user's search string.
        """
        self.check_budget()
        if isinstance(snapshot, list):
            for item in snapshot:
                self.admit_snapshot(item)
            return
        if not isinstance(snapshot, dict):
            return
        binding = snapshot.get("binding")
        failed = snapshot.get("failed_attempt")
        if binding or failed:
            identity = binding or failed
            generation = snapshot.get("evaluation_generation") or identity.get(
                "generation"
            )
            key = digest(
                {
                    "identity": identity,
                    "generation": generation,
                    "schema": snapshot.get("run_schema_version"),
                    "config": snapshot.get("run_config_sha256"),
                }
            )
            if key not in self.admission_checked:
                if len(self.admission_checked) >= 256:
                    raise DomainError(
                        "note_evidence_capacity",
                        "Note corpus exceeds 256 distinct physical admission bindings; narrow its scope",
                        422,
                    )
                run = self.session.scalar(
                    select(ResearchRun).where(
                        ResearchRun.id == UUID(identity["run_id"]),
                        ResearchRun.owner_id == self.owner_id,
                    )
                )
                if (
                    run is None
                    and snapshot.get("run_schema_version") in PROTECTED_SCHEMAS
                ):
                    raise DomainError(
                        "note_admission_unavailable",
                        "Original protected execution authority is unavailable; note outcomes are not released",
                        409,
                    )
                if run is not None:
                    evaluation = (
                        self.session.scalar(
                            select(Evaluation).where(
                                Evaluation.id == UUID(identity["evaluation_id"]),
                                Evaluation.run_id == run.id,
                            )
                        )
                        if identity.get("evaluation_id")
                        else None
                    )
                    projection = SimpleNamespace(
                        **{
                            column.name: getattr(run, column.name)
                            for column in ResearchRun.__table__.columns
                        }
                    )
                    projection.generation = generation or run.generation
                    schema = run.config.get("schema_version")
                    expected_schema = snapshot.get("run_schema_version")
                    expected_hash = snapshot.get("run_config_sha256") or identity.get(
                        "config_sha256"
                    )
                    if expected_schema != schema or expected_hash != run.request_sha256:
                        raise DomainError(
                            "note_admission_unavailable",
                            "Original execution schema or configuration changed; note outcomes are not released",
                            409,
                        )
                    try:
                        protected = schema in PROTECTED_SCHEMAS
                        if protected and failed:
                            stopped = self.session.scalar(
                                select(ExecutionAttempt).where(
                                    ExecutionAttempt.id
                                    == UUID(identity["execution_attempt_id"]),
                                    ExecutionAttempt.run_id == run.id,
                                )
                            )
                            if (
                                stopped is None
                                or stopped.generation != generation
                                or stopped.status != identity.get("status")
                                or stopped.status
                                not in {"failed", "cancelled", "interrupted"}
                                or stopped.finished_at is None
                                or not stopped.sandbox_stopped
                            ):
                                raise DomainError(
                                    "result_unavailable",
                                    "Original stopped control attempt is unavailable",
                                    409,
                                )
                        if protected and not failed:
                            if evaluation is None:
                                raise DomainError(
                                    "result_unavailable",
                                    "Original evaluation authority is unavailable",
                                    409,
                                )
                            payload = evaluation.payload
                            result_key = (
                                "portable_result"
                                if schema.startswith("tradegrad.portable.")
                                else "result_bundle"
                            )
                            if (
                                str(evaluation.attempt_id)
                                != identity.get("execution_attempt_id")
                                or evaluation.generation != generation
                                or evaluation.code_sha256 != identity.get("code_sha256")
                                or not isinstance(payload, dict)
                                or payload_hash(payload) != evaluation.payload_sha256
                                or not isinstance(payload.get(result_key), dict)
                                or payload[result_key].get("sha256")
                                != identity.get("result_manifest_sha256")
                            ):
                                raise DomainError(
                                    "result_unavailable",
                                    "Original evaluation binding changed",
                                    409,
                                )
                            from backend.app.final_evaluation_views import (
                                validate_final_terminal,
                            )

                            attempt = self.session.scalar(
                                select(ExecutionAttempt).where(
                                    ExecutionAttempt.id == evaluation.attempt_id,
                                    ExecutionAttempt.run_id == run.id,
                                )
                            )
                            if attempt is None:
                                raise DomainError(
                                    "result_unavailable",
                                    "Original stopped attempt is unavailable",
                                    409,
                                )
                            validate_final_terminal(evaluation, run, attempt)
                        if schema in {
                            "tradegrad.final-evaluation.v1",
                            "tradegrad.portable.final-evaluation.v1",
                        }:
                            from backend.app.final_result_admission import (
                                require_final_result_admission,
                            )

                            if schema == "tradegrad.final-evaluation.v1":
                                from backend.app.final_evaluation_services import (
                                    validate_final_binding,
                                )
                            else:
                                from backend.app.portable_final_services import (
                                    validate_final_binding,
                                )
                            _, _, dataset, _, _ = validate_final_binding(
                                self.session, run
                            )
                            require_final_result_admission(
                                self.session,
                                run,
                                evaluation,
                                self.owner_id,
                                dataset=dataset,
                            )
                        elif (
                            run.config.get("execution_mode") == "independent_evaluation"
                        ):
                            from backend.app.independent_services import (
                                owned_independent,
                                require_independent_admission,
                            )

                            owned_independent(self.session, run.id, self.owner_id)
                            require_independent_admission(
                                self.session, projection if failed else run, evaluation
                            )
                        from backend.app.result_access import record_result_access

                        record_result_access(
                            self.session,
                            projection,
                            self.owner_id,
                            evaluation=evaluation,
                            purpose=self.purpose,
                            source="research-note-metadata",
                            conservative=evaluation is None,
                        )
                    except DomainError as error:
                        raise DomainError(
                            "note_admission_unavailable",
                            "Original evidence admission is missing or invalid; note outcome content is not released",
                            409,
                        ) from error
                self.admission_checked.add(key)
        for key, value in snapshot.items():
            if key not in {"binding", "failed_attempt"} and isinstance(
                value, (dict, list)
            ):
                self.admit_snapshot(value)

    def pin(self, kind, identifier, sha=None, **details):
        return {
            "resource_kind": kind,
            "resource_id": str(identifier),
            "sha256": sha,
            "details": details,
        }

    def artifact(self, item):
        return self.pin(
            "artifact",
            item.id,
            item.sha256,
            storage_key=item.storage_key,
            size_bytes=item.size_bytes,
            name=item.name,
            run_id=str(item.run_id),
            attempt_id=str(item.attempt_id),
            media_type=item.media_type,
        )

    def verify_artifact(self, item):
        # Streaming, no symlink traversal or caller-provided paths. Every referenced
        # block is checked, not merely the manifest. The fixed inventory bounds
        # metadata and traversal; this does not truncate the immutable evidence.
        key = (item.id, item.sha256)
        if key not in self.verified_files:
            self.check_budget()
            self.total_verified_bytes += item.size_bytes
            if (
                len(self.verified_files) >= MAX_PINS
                or self.total_verified_bytes > 8 * 1024**3
            ):
                raise DomainError(
                    "note_evidence_capacity",
                    "Evidence admission exceeds its 100000-file / 8 GiB read budget; narrow the note page",
                    422,
                )
            try:
                with _verified_file(
                    safe_path(self.settings.storage_root, item.storage_key), item
                ):
                    pass
            except TraceStorageError as error:
                raise DomainError(error.code, error.message, 409) from None
            self.verified_files.add(key)

    def source(self, pin):
        resource = owned_source(
            self.session, pin.source_kind, pin.source_id, self.owner_id
        )
        if resource.code_sha256 != pin.code_sha256:
            raise mismatch(
                "Pinned source code hash differs; no new code was substituted"
            )
        pins = [self.pin(pin.source_kind, pin.source_id, pin.code_sha256)]
        pins.append(
            self.pin(
                "strategy_version",
                resource.strategy.id,
                resource.strategy.content_sha256,
            )
        )
        snapshot = {
            "strategy_version_id": str(resource.strategy.id),
            "source_kind": pin.source_kind,
            "source_id": str(pin.source_id),
            "code_sha256": pin.code_sha256,
        }
        spec = None
        if pin.spec_revision is not None:
            row = self.session.scalar(
                select(StrategySpecRevision).where(
                    _clause(resource),
                    StrategySpecRevision.owner_id == self.owner_id,
                    StrategySpecRevision.revision == pin.spec_revision,
                )
            )
            if row is None:
                raise DomainError(
                    "not_found", "Pinned description revision not found", 404
                )
            if row.spec_sha256 != pin.spec_sha256:
                raise mismatch("Pinned description hash differs")
            spec = _spec_public(row, resource).spec
            pins.append(self.pin("spec_revision", row.id, row.spec_sha256))
            snapshot.update(
                spec_revision=pin.spec_revision, spec_sha256=pin.spec_sha256
            )
        rule_id = getattr(pin, "rule_id", None)
        if rule_id is not None and (
            spec is None or not any(rule.rule_id == rule_id for rule in spec.rules)
        ):
            raise DomainError("not_found", "Rule not found in pinned description", 404)
        if rule_id is not None:
            snapshot["rule_stage_id"] = next(
                rule.stage_id for rule in spec.rules if rule.rule_id == rule_id
            )
        anchor = getattr(pin, "source_ref", None)
        if anchor is not None:
            self.anchor(anchor, resource.item.code, pin.code_sha256)
            if (
                rule_id is not None
                and next(
                    rule for rule in spec.rules if rule.rule_id == rule_id
                ).source_ref
                != anchor
            ):
                raise mismatch("Rule and source anchor differ")
        return snapshot, pins

    @staticmethod
    def anchor(anchor, code, code_sha256):
        try:
            locations = source_locations(code)
            location = locations.get(anchor.symbol)
            if (
                anchor.code_sha256 != code_sha256
                or anchor.file != "strategy.py"
                or location is None
                or not location[0]
                <= anchor.line_start
                <= anchor.line_end
                <= location[1]
            ):
                raise ValueError("Source anchor differs")
        except (ValueError, SyntaxError, TypeError):
            raise mismatch("Source anchor does not identify this exact code") from None

    def leaf(self, pin, *, depth=0):
        key = digest(pin)
        if key in self.leaves:
            return self.leaves[key]
        if depth >= 16:
            raise DomainError(
                "independent_source_depth",
                "Evidence ancestry exceeds bounded read admission",
                409,
            )
        from backend.app.independent_services import _source_cache

        side = None
        for cache_key, cached in (_source_cache.get() or {}).items():
            if cache_key[:2] != (id(self.session), str(self.owner_id)):
                continue
            admitted = cached.admitted
            bound = admitted.input.binding
            candidate = ComparisonSideCreate(
                evaluation_id=bound.evaluation_id,
                execution_attempt_id=bound.execution_attempt_id,
                result_manifest_sha256=bound.result_manifest_sha256,
                trace_manifest_sha256=admitted.trace.context.trace_manifest_sha256,
                source_kind=admitted.source.source_kind,
                source_id=admitted.source.source_id,
                code_sha256=bound.code_sha256,
                spec_revision=admitted.source.spec.revision
                if admitted.source.spec
                else None,
                spec_sha256=admitted.source.spec_sha256,
            )
            if candidate == pin:
                side = admitted
                break
        if side is None:
            self.check_leaf_budget(pin)
            side = load_comparison_side(self.session, pin, self.owner_id, self.settings)
        self.check_budget()
        snapshot, pins = self.source(pin)
        result = side.trace.result
        binding = side.input.binding
        snapshot["binding"] = binding.model_dump(mode="json")
        snapshot["evaluation_generation"] = result.evaluation.generation
        snapshot["trace_manifest_sha256"] = side.trace.context.trace_manifest_sha256
        snapshot["run_schema_version"] = result.run.config.get("schema_version")
        snapshot["run_config_sha256"] = result.run.request_sha256
        pins.extend(
            [
                self.pin("run", result.run.id, result.run.request_sha256),
                self.pin(
                    "evaluation", result.evaluation.id, result.evaluation.payload_sha256
                ),
                self.pin(
                    "dataset",
                    result.dataset.id,
                    result.dataset.content_sha256,
                    storage_key=result.dataset.storage_key,
                ),
            ]
        )
        if binding.universe_version_id is not None:
            pins.append(
                self.pin(
                    "universe", binding.universe_version_id, binding.universe_sha256
                )
            )
        if result.run.experiment_id:
            pins.append(self.pin("experiment", result.run.experiment_id))
        names = {manifest_name(result.evaluation.id)}
        if side.trace.manifest:
            names.update(ref.name for ref in side.trace.manifest.artifacts)
        for suffix in ("runtime-observation.json", "diagnostic-parity.json"):
            names.add(f"evaluation-{result.evaluation.id}-{suffix}")
        artifacts = self.session.scalars(
            select(Artifact).where(
                Artifact.run_id == result.run.id,
                Artifact.attempt_id == result.evaluation.attempt_id,
                or_(
                    Artifact.id == result.artifact.id, Artifact.name.in_(sorted(names))
                ),
            )
        ).all()
        for item in artifacts:
            self.verify_artifact(item)
            pins.append(self.artifact(item))
        if result.run.config.get("execution_mode") == "independent_evaluation":
            from backend.app.independent_services import independent_config

            config = independent_config(result.run)
            original_pin = ComparisonSideCreate.model_validate(
                {
                    name: getattr(config.source, name)
                    for name in ComparisonSideCreate.model_fields
                }
            )
            _, original_snapshot, inherited = self.leaf(original_pin, depth=depth + 1)
            snapshot["ancestors"] = [original_snapshot]
            pins.extend(inherited)
        record_verified_result_access(
            self.session, result, self.owner_id, purpose=self.purpose
        )
        value = (side, snapshot, pins)
        self.leaves[key] = value
        return value

    def decision(self, ref, side):
        trace = side.trace
        if trace.reader is None:
            raise DomainError(
                "note_evidence_unavailable",
                "Pinned trace has no saved decision observations",
                409,
            )
        event = _event(trace, ref.decision_id)
        if ref.session_date and ref.session_date != event["start"].isoformat():
            raise mismatch("Pinned decision and session date differ")
        stages = _query(trace, "stages", **event, stage_id=ref.stage_id, limit=100)
        if ref.stage_id and not stages["total"]:
            raise DomainError("not_found", "Stage not found in pinned decision", 404)
        if stages["total"] > 100:
            raise DomainError(
                "trace_integrity", "Pinned decision has too many stages", 409
            )
        if ref.record_kind:
            table, key = {
                "candidate": ("candidates", "instrument_id"),
                "feature": ("features", "feature_id"),
                "predicate": ("predicates", "condition_id"),
                "order": (None, "logical_order_id"),
            }[ref.record_kind]
            if table is None:
                records = [
                    (stage["stage_id"], ordinal, link)
                    for stage in stages["items"]
                    for ordinal, link in enumerate(stage["payload"]["order_links"])
                ]
            else:
                page = _query(
                    trace,
                    table,
                    **event,
                    stage_id=ref.stage_id,
                    instrument_id=ref.instrument_id,
                    record_ordinal=ref.record_ordinal,
                    limit=500,
                )
                if page["total"] > 500:
                    raise DomainError(
                        "note_evidence_unavailable",
                        "Use an exact recorded ordinal for this large observation set",
                        422,
                    )
                records = [
                    (row["stage_id"], row["ordinal"], row["payload"])
                    for row in page["items"]
                ]
            matches = [
                value
                for stage, ordinal, value in records
                if value.get(key) == ref.record_id
                and (ref.record_ordinal is None or ordinal == ref.record_ordinal)
                and (
                    ref.instrument_id is None
                    or value.get("instrument_id") == ref.instrument_id
                )
            ]
            if len(matches) != 1:
                raise DomainError(
                    "note_evidence_unavailable",
                    "Pinned observation is missing or ambiguous; no first-row fallback is allowed",
                    409,
                )
            if ref.source_ref and matches[0].get(
                "source_ref"
            ) != ref.source_ref.model_dump(mode="json"):
                raise mismatch("Observation and source anchor differ")
        elif ref.instrument_id:
            # Instruments must actually occur in this decision, not just the dataset.
            found = any(
                _query(
                    trace,
                    table,
                    **event,
                    instrument_id=ref.instrument_id,
                    stage_id=ref.stage_id,
                    limit=1,
                )["total"]
                for table in ("candidates", "features", "predicates")
            ) or any(
                link.get("instrument_id") == ref.instrument_id
                for stage in stages["items"]
                for link in stage["payload"]["order_links"]
            )
            decision = _query(trace, "decisions", **event, limit=2)
            if decision["total"] != 1:
                raise DomainError(
                    "trace_integrity", "Decision identity is ambiguous", 409
                )
            payload = decision["items"][0]["payload"]
            portfolios = (payload.get("pre_state") or {}).get("positions") or []
            portfolios = list(portfolios)
            portfolios += [
                item
                for stage in stages["items"]
                for key in ("raw_targets", "constrained_plan")
                for item in stage["payload"].get(key) or []
            ]
            found = found or any(
                item.get("instrument_id") == ref.instrument_id for item in portfolios
            )
            if not found:
                raise DomainError(
                    "not_found",
                    "Instrument not observed in pinned decision selection",
                    404,
                )
        if ref.source_ref:
            self.anchor(ref.source_ref, side.source.code, ref.leaf.code_sha256)

    def resolve(self, ref):
        self.check_budget()
        key = digest(ref)
        if key in self.resolved:
            return self.resolved[key]
        self.resolve_calls += 1
        if self.resolve_calls > 128:
            raise DomainError(
                "note_evidence_capacity",
                "Note page exceeds 128 distinct evidence references; reduce its page size",
                422,
            )
        if ref.kind == "source":
            snapshot, pins = self.source(ref)
            value = ResolvedEvidence(snapshot, pins=pins)
        elif ref.kind in {"evaluation", "decision"}:
            side, snapshot, pins = self.leaf(ref.leaf)
            if ref.kind == "decision":
                self.decision(ref, side)
            elif ref.source_ref:
                self.anchor(ref.source_ref, side.source.code, ref.leaf.code_sha256)
            value = ResolvedEvidence(snapshot, [side.input.binding], pins)
        elif ref.kind == "comparison":
            self.check_leaf_budget(ref.left)
            self.check_leaf_budget(ref.right)
            result = load_owned_comparison_result(
                self.session,
                ref.comparison_id,
                self.owner_id,
                self.settings,
                manifest_sha256=ref.manifest_sha256,
            )
            config = comparison_config(result.run, result.comparison)
            if (
                result.comparison.input_sha256 != ref.input_sha256
                or config.request.left != ref.left
                or config.request.right != ref.right
            ):
                raise mismatch("Comparison input or exact two-sided pins differ")
            sides = [self.leaf(pin) for pin in (ref.left, ref.right)]
            if ref.event_pair_id:
                page = comparison_section_view(
                    result, "events", event_pair_id=ref.event_pair_id, limit=2
                )
                if page.total != 1:
                    raise DomainError(
                        "not_found", "Event pair not found in pinned comparison", 404
                    )
            if ref.instrument_id:
                selected = comparison_section_view(
                    result,
                    ref.section,
                    event_pair_id=ref.event_pair_id,
                    instrument_id=ref.instrument_id,
                    limit=1,
                )
                if not selected.total:
                    raise DomainError(
                        "not_found",
                        "Instrument not found in pinned comparison section",
                        404,
                    )
            if ref.mechanism_index is not None and ref.mechanism_index >= len(
                result.manifest.mechanism_changes
            ):
                raise DomainError(
                    "not_found", "Mechanism not found in pinned comparison", 404
                )
            names = [item.name for item in result.manifest.artifacts]
            artifacts = self.session.scalars(
                select(Artifact).where(
                    Artifact.run_id == result.run.id,
                    Artifact.attempt_id == result.comparison.published_attempt_id,
                    or_(Artifact.id == result.artifact.id, Artifact.name.in_(names)),
                )
            ).all()
            pins = [pin for _, _, group in sides for pin in group]
            for item in artifacts:
                self.verify_artifact(item)
                pins.append(self.artifact(item))
            pins.extend(
                [
                    self.pin("comparison", result.comparison.id, ref.input_sha256),
                    self.pin("run", result.run.id, result.run.request_sha256),
                ]
            )
            snapshot = {
                "strategy_version_id": sides[0][1]["strategy_version_id"],
                "sources": [item[1] for item in sides],
                "published_attempt_id": str(result.comparison.published_attempt_id),
                "manifest_artifact_id": str(result.artifact.id),
                "manifest_sha256": result.artifact.sha256,
                "input_sha256": ref.input_sha256,
            }
            release_comparison_access(
                self.session, result, self.owner_id, purpose=self.purpose
            )
            value = ResolvedEvidence(
                snapshot, [item[0].input.binding for item in sides], pins
            )
        elif ref.kind == "diagnostic":
            from backend.app.diagnostic_parity_views import load_owned_parity
            from backend.app.independent_services import (
                independent_config,
                owned_independent,
            )

            item, run = owned_independent(self.session, ref.run_id, self.owner_id)
            config = independent_config(run)
            if item.purpose != "diagnostic" or config.sha256 != ref.config_sha256:
                raise mismatch("Diagnostic purpose or immutable configuration differs")
            side, snapshot, pins = self.leaf(ref.result)
            if side.trace.result.run.id != run.id:
                raise mismatch("Diagnostic physical result belongs to another run")
            report, sha = load_owned_parity(
                self.session,
                run.id,
                self.owner_id,
                self.settings,
                report_sha256=ref.report_sha256,
            )
            if report.replay.binding != side.input.binding:
                raise mismatch("Diagnostic report and physical result differ")
            original_pin = ComparisonSideCreate.model_validate(
                {
                    name: getattr(config.source, name)
                    for name in ComparisonSideCreate.model_fields
                }
            )
            _, original, inherited = self.leaf(original_pin)
            pins = [*pins, *inherited]
            snapshot = {
                **snapshot,
                "strategy_version_id": original["strategy_version_id"],
                "original": original,
                "original_pin": original_pin.model_dump(mode="json"),
                "diagnostic_config_sha256": ref.config_sha256,
                "report_sha256": sha,
            }
            value = ResolvedEvidence(snapshot, [side.input.binding], pins)
        else:
            value = self.control(ref)
        if (
            len(value.pins) > MAX_PINS
            or len(canonical_bytes(value.snapshot)) > 4 * 1024**2
        ):
            raise DomainError(
                "note_evidence_capacity",
                "Evidence inventory exceeds its explicit bounded capacity",
                422,
            )
        root, selection = reference_selection(ref, value.snapshot)
        value.href = (
            href(root, selection)
            if selection.get("spec_revision")
            and selection.get("spec_sha256")
            and selection.get("code_sha256")
            else None
        )
        if ref.kind == "controlled" and any(
            arm.status != "succeeded" for arm in ref.arms
        ):
            value.href = None
        self.resolved[key] = value
        return value

    def control(self, ref):
        from backend.app.independent_services import (
            independent_config,
            owned_independent,
            require_independent_admission,
            validate_independent_inputs,
        )
        from backend.app.independent_views import owned_controlled

        item = owned_controlled(
            self.session, ref.controlled_experiment_id, self.owner_id
        )
        if (
            item.request_sha256 != ref.request_sha256
            or item.protocol_sha256 != ref.protocol_sha256
        ):
            raise mismatch("Controlled request or fixed protocol differs")
        arms = self.session.scalars(
            select(ControlledArm)
            .where(ControlledArm.controlled_experiment_id == item.id)
            .order_by(ControlledArm.ordinal)
        ).all()
        if len(arms) != len(ref.arms):
            raise mismatch("Controlled evidence must include every original arm")
        original_pin = ComparisonSideCreate.model_validate(
            {
                name: item.request["source"].get(name)
                for name in ComparisonSideCreate.model_fields
            }
        )
        _, source_snapshot, pins = self.leaf(original_pin)
        pins = list(pins)
        sources, bindings = [], []
        for arm, pin in zip(arms, ref.arms, strict=True):
            execution = self.session.scalar(
                select(IndependentEvaluation).where(
                    IndependentEvaluation.id == arm.independent_id,
                    IndependentEvaluation.owner_id == self.owner_id,
                )
            )
            if execution is None:
                raise DomainError("not_found", "Controlled arm source not found", 404)
            _, run = owned_independent(self.session, execution.run_id, self.owner_id)
            if arm.ordinal != pin.ordinal or run.id != pin.run_id:
                raise mismatch("Controlled ordinal and run differ")
            config = independent_config(run)
            if (
                execution.purpose != "controlled"
                or execution.protocol_sha256 != item.protocol_sha256
                or run.experiment_id != item.experiment_id
                or run.split_version_id != item.split_version_id
            ):
                raise mismatch("Controlled arm does not belong to the fixed protocol")
            validate_independent_inputs(
                self.session, run, self.settings, execution=False
            )
            original_run = SimpleNamespace(
                **{
                    column.name: getattr(run, column.name)
                    for column in ResearchRun.__table__.columns
                }
            )
            original_run.generation = pin.generation
            require_independent_admission(self.session, original_run)
            attempt = self.session.scalar(
                select(ExecutionAttempt).where(
                    ExecutionAttempt.id == pin.execution_attempt_id,
                    ExecutionAttempt.run_id == run.id,
                )
            )
            if attempt is None:
                raise DomainError(
                    "not_found", "Pinned controlled attempt no longer exists", 404
                )
            if (
                attempt.generation != pin.generation
                or attempt.status != pin.status
                or not attempt.sandbox_stopped
                or attempt.finished_at is None
            ):
                raise mismatch(
                    "Controlled evidence requires the exact terminal stopped attempt"
                )
            if pin.result:
                side, snapshot, inventory = self.leaf(pin.result)
                if side.trace.result.run.id != run.id:
                    raise mismatch("Controlled result belongs to another arm")
                bindings.append(side.input.binding)
                pins.extend(inventory)
            else:
                snapshot = {
                    "run_schema_version": run.config.get("schema_version"),
                    "run_config_sha256": run.request_sha256,
                    "failed_attempt": {
                        "run_id": str(run.id),
                        "status": pin.status,
                        "generation": pin.generation,
                        "execution_attempt_id": str(attempt.id),
                        "config_sha256": config.sha256,
                    },
                }
                pins.append(self.pin("run", run.id, run.request_sha256))
            sources.append(snapshot)
        pins.extend(
            [
                self.pin("controlled_experiment", item.id, item.request_sha256),
                self.pin("experiment", item.experiment_id),
            ]
        )
        snapshot = {
            "strategy_version_id": source_snapshot["strategy_version_id"],
            "source": source_snapshot,
            "original_pin": original_pin.model_dump(mode="json"),
            "arms": sources,
            "request_sha256": item.request_sha256,
            "protocol_sha256": item.protocol_sha256,
        }
        return ResolvedEvidence(snapshot, bindings, pins)
