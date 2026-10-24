import { useCallback, useEffect, useState } from "react";
import {
  Link,
  useNavigate,
  useParams,
  useSearch,
} from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import type { WorkbenchEvaluation, WorkbenchSpec } from "@/client/types.gen";
import { ErrorState, Loading, PageHeader, Panel } from "@/components/common";
import { Metadata, Pagination } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { currentUserOptions, strategyOptions } from "@/lib/api";
import { candidateOptions } from "@/lib/research-api";
import { decisionMatches } from "@/lib/replay-evidence";
import {
  evidenceMatches,
  leafSelection,
  leafBindingProblem,
  workbenchRunTarget,
} from "@/lib/workbench-evidence";
import {
  changeSelection,
  clearComparisonSide,
  clearComparisonResult,
  comparisonSidePatch,
  clearedReplaySelection,
  resourceSelection,
  selectionProblem,
  workbenchTabs,
  type WorkbenchSelection,
} from "@/lib/workbench-selection";
import {
  selectedDecisionOptions,
  workbenchEvaluationOptions,
  workbenchEvaluationsOptions,
  workbenchLineageOptions,
  workbenchSpecOptions,
  workbenchTraceOptions,
  type SourceResource,
} from "@/lib/workbench-api";
import { AnchoredSource } from "./anchored-source";
import { DecisionReplay } from "./decision-replay";
import { MechanismComparison } from "./comparison";
import { StrategyVerification } from "./verification";
import { sideFromEvidence } from "@/lib/comparison-evidence";
import { DescriptionRevisions } from "./description-revisions";
import { LineageSidebar } from "./lineage";
import { RuleCards } from "./rule-cards";
import { RuleGraph } from "./rule-graph";
import "./workbench.css";

const labels = {
  description: "Description",
  replay: "Decision replay",
  comparison: "Version comparison",
  verification: "Verification",
  notes: "Notes",
};
export function ResearchWorkbenchPage() {
  const { strategyId } = useParams({
    from: "/workspace/strategies/$strategyId/research",
  });
  const selection = useSearch({
    from: "/workspace/strategies/$strategyId/research",
  });
  const user = useQuery(currentUserOptions);
  return (
    <>
      <PageHeader
        title="Strategy research workbench"
        subtitle="Understand an exact source version, select a physical leaf execution, and retain immutable research evidence."
      />
      {user.isPending ? (
        <Loading />
      ) : user.error ? (
        <ErrorState error={user.error} />
      ) : (
        user.data && (
          <Workbench
            key={`${strategyId}:${selection.candidate_id || "strategy"}`}
            strategyId={strategyId}
            selection={selection}
            owner={user.data.id}
          />
        )
      )}
    </>
  );
}
function Workbench({
  strategyId,
  selection,
  owner,
}: {
  strategyId: string;
  selection: WorkbenchSelection;
  owner: string;
}) {
  const navigate = useNavigate({ from: "/strategies/$strategyId/research" });
  const source: SourceResource = {
    kind: selection.candidate_id ? "candidate" : "strategy_version",
    id: selection.candidate_id || strategyId,
  };
  const [lineageOffset, setLineageOffset] = useState(0);
  const [evaluationOffset, setEvaluationOffset] = useState(0);
  const problem = selectionProblem(selection);
  const strategy = useQuery(strategyOptions(strategyId));
  const candidate = useQuery(candidateOptions(selection.candidate_id || ""));
  const spec = useQuery({
    ...workbenchSpecOptions(source, selection),
    enabled: !problem,
  });
  const lineage = useQuery({
    ...workbenchLineageOptions(source, lineageOffset),
    enabled: !problem,
  });
  const evaluations = useQuery({
    ...workbenchEvaluationsOptions(source, evaluationOffset),
    enabled: !problem,
  });
  const leaf = useQuery({
    ...workbenchEvaluationOptions(selection.evaluation_id),
    enabled: Boolean(selection.evaluation_id) && !problem,
  });
  const currentSource = selection.candidate_id
    ? candidate.data?.code
    : strategy.data?.code;
  const currentHash = selection.candidate_id
    ? candidate.data?.code_sha256
    : strategy.data?.content_sha256;
  const sourceProblem =
    spec.data &&
    (spec.data.strategy_version_id !== strategyId ||
      spec.data.resource_id !== source.id ||
      (currentHash && spec.data.code_sha256 !== currentHash) ||
      (selection.code_sha256 &&
        spec.data.code_sha256 !== selection.code_sha256))
      ? "Description, source and requested code hashes differ. No newer source or explanation was substituted."
      : null;
  const bindingProblem = leafBindingProblem(selection, strategyId, leaf.data);
  const leafVerified = Boolean(
    leaf.data &&
    !leaf.error &&
    !bindingProblem &&
    !sourceProblem &&
    spec.data &&
    currentHash &&
    selection.code_sha256 === currentHash,
  );
  const trace = useQuery({
    ...workbenchTraceOptions(selection),
    enabled:
      !problem &&
      leafVerified &&
      Boolean(
        selection.evaluation_id &&
        selection.execution_attempt_id &&
        selection.result_manifest_sha256 &&
        (selection.trace_status !== "not_collected" ||
          Boolean(selection.trace_manifest_sha256)),
      ),
  });
  const detail = useQuery({
    ...selectedDecisionOptions(selection),
    enabled:
      !problem &&
      leafVerified &&
      Boolean(
        selection.evaluation_id &&
        selection.execution_attempt_id &&
        selection.result_manifest_sha256 &&
        selection.trace_manifest_sha256 &&
        selection.decision_id &&
        selection.stage_id &&
        (selection.tab || "description") === "description",
      ),
  });
  const activeTab = selection.tab || "description";
  const selectedRule =
    spec.data?.spec.rules.find((rule) => rule.rule_id === selection.rule_id) ||
    (!selection.rule_id
      ? spec.data?.spec.rules.find(
          (rule) => rule.stage_id === selection.stage_id,
        )
      : undefined);
  const update = useCallback(
    (patch: Partial<WorkbenchSelection>, replace = false) =>
      void navigate({
        search: (previous) => changeSelection(previous, patch),
        replace,
      }),
    [navigate],
  );
  const selectRule = useCallback(
    (id: string) => {
      const rule = spec.data?.spec.rules.find((item) => item.rule_id === id);
      if (rule) update({ rule_id: id, stage_id: rule.stage_id ?? undefined });
    },
    [spec.data, update],
  );
  useEffect(() => {
    if (problem || sourceProblem || !spec.data) return;
    if (!selection.spec_revision || !selection.code_sha256)
      update(
        {
          spec_revision: spec.data.revision,
          spec_sha256: spec.data.spec_sha256,
          code_sha256: spec.data.code_sha256,
        },
        true,
      );
  }, [
    spec.data,
    selection.spec_revision,
    selection.code_sha256,
    problem,
    sourceProblem,
    update,
  ]);
  useEffect(() => {
    if (
      problem ||
      bindingProblem ||
      !leaf.data ||
      selection.execution_attempt_id ||
      selection.result_manifest_sha256
    )
      return;
    update(
      {
        execution_attempt_id: leaf.data.execution_attempt_id,
        result_manifest_sha256: leaf.data.result_manifest_sha256 ?? undefined,
        trace_manifest_sha256: leaf.data.trace_manifest_sha256 ?? undefined,
        trace_status: leaf.data.trace_manifest_sha256
          ? "available"
          : "not_collected",
      },
      true,
    );
  }, [
    leaf.data,
    selection.execution_attempt_id,
    selection.result_manifest_sha256,
    problem,
    bindingProblem,
    update,
  ]);
  useEffect(() => {
    if (
      leafVerified &&
      trace.data &&
      ((!selection.trace_manifest_sha256 && !selection.trace_status) ||
        selection.trace_status !== trace.data.context.status) &&
      evidenceMatches(selection, trace.data.context)
    )
      update(
        {
          trace_manifest_sha256:
            trace.data.context.trace_manifest_sha256 ?? undefined,
          trace_status: trace.data.context.status,
        },
        true,
      );
  }, [trace.data, selection, update, leafVerified]);
  const trustedDetail =
    leafVerified && detail.data && decisionMatches(selection, detail.data)
      ? detail.data
      : undefined;
  function openSource(value: SourceResource, seedId: string) {
    void navigate({
      params: { strategyId: seedId },
      search: resourceSelection(
        selection,
        value.kind === "candidate" ? value.id : undefined,
      ),
    });
  }
  function selectSpec(value: WorkbenchSpec) {
    update({
      spec_revision: value.revision,
      spec_sha256: value.spec_sha256,
      code_sha256: value.code_sha256,
    });
  }
  function selectLeaf(value: WorkbenchEvaluation) {
    void navigate({ search: leafSelection(selection, value) });
  }
  if (problem || sourceProblem)
    return <ErrorState error={new Error(problem || sourceProblem!)} />;
  if (
    strategy.error ||
    (selection.candidate_id && candidate.error) ||
    spec.error
  )
    return (
      <ErrorState error={strategy.error || candidate.error || spec.error} />
    );
  if (!spec.data || currentSource === undefined || !currentHash)
    return <Loading text="Loading exact source and description…" />;
  const pinnedTrace =
    leafVerified &&
    (selection.trace_status !== "not_collected" ||
      Boolean(selection.trace_manifest_sha256)) &&
    trace.data &&
    evidenceMatches(selection, trace.data.context)
      ? trace.data
      : undefined;
  const statusTarget = leaf.data ? workbenchRunTarget(leaf.data) : null;
  const leaves = evaluations.data?.data || [];
  const choices =
    leaf.data &&
    !leaves.some((value) => value.evaluation_id === leaf.data!.evaluation_id)
      ? [leaf.data, ...leaves]
      : leaves;
  return (
    <div className="workbench-layout">
      {lineage.isPending ? (
        <Loading text="Loading version lineage…" />
      ) : lineage.error ? (
        <ErrorState error={lineage.error} />
      ) : (
        lineage.data && (
          <LineageSidebar
            lineage={lineage.data}
            offset={lineageOffset}
            busy={lineage.isFetching}
            onPage={setLineageOffset}
            onOpen={openSource}
            onCompareParent={() => {
              const parent = lineage.data?.parent;
              if (!parent) return;
              update({
                ...clearComparisonSide("left"),
                ...clearComparisonSide("right"),
                ...clearComparisonResult(),
                tab: "comparison",
                ...comparisonSidePatch("left", {
                  source_kind: parent.resource_kind,
                  source_id: parent.id,
                  strategy_version_id: parent.strategy_version_id,
                  code_sha256: parent.code_sha256,
                }),
                ...comparisonSidePatch(
                  "right",
                  sideFromEvidence(
                    spec.data!,
                    leafVerified ? leaf.data : undefined,
                  ),
                ),
              });
            }}
          />
        )
      )}
      <div className="workbench-main">
        <Panel title="Immutable research selection">
          <div className="workbench-pins" data-testid="workbench-selection">
            <Metadata
              values={{
                "Strategy version": strategyId,
                Candidate:
                  selection.candidate_id ||
                  "No candidate selected; saved strategy source",
                "Source code SHA-256": currentHash,
                "Description revision": spec.data.revision,
                "Description SHA-256": spec.data.spec_sha256,
                Mode: lineage.data?.focus.mode || strategy.data?.mode,
                "Actual leaf Evaluation":
                  selection.evaluation_id ||
                  "No actual leaf Evaluation selected",
                "Execution attempt":
                  selection.execution_attempt_id || "Not selected",
                "Result manifest SHA-256":
                  selection.result_manifest_sha256 || "No result pinned",
                "Trace manifest SHA-256":
                  selection.trace_manifest_sha256 || "No trace pinned",
                Decision: selection.decision_id || "Not selected",
                Instrument:
                  selection.instrument_id || "Whole decision / not selected",
                Stage: selection.stage_id || "Not selected",
              }}
            />
          </div>
          <label className="workbench-field">
            Actual leaf Evaluation
            <select
              value={selection.evaluation_id || ""}
              onChange={(event) => {
                if (!event.target.value) {
                  update({
                    ...clearedReplaySelection(),
                    evaluation_id: undefined,
                    execution_attempt_id: undefined,
                    result_manifest_sha256: undefined,
                    trace_manifest_sha256: undefined,
                    trace_status: undefined,
                    decision_id: undefined,
                    instrument_id: undefined,
                    stage_id: undefined,
                  });
                  return;
                }
                const value = choices.find(
                  (item) => item.evaluation_id === event.target.value,
                );
                if (value) selectLeaf(value);
              }}
            >
              <option value="">No actual leaf Evaluation selected</option>
              {choices.map((value) => (
                <option key={value.evaluation_id} value={value.evaluation_id}>
                  {value.split} · {value.status} · {value.evaluation_id}
                </option>
              ))}
            </select>
          </label>
          {evaluations.error && <ErrorState error={evaluations.error} />}
          {evaluations.data && (
            <Pagination
              offset={evaluationOffset}
              limit={25}
              count={evaluations.data.count}
              busy={evaluations.isFetching}
              onPage={setEvaluationOffset}
            />
          )}
          {leaf.error && <ErrorState error={leaf.error} />}
          {bindingProblem && <ErrorState error={new Error(bindingProblem)} />}
          {leaf.data && !bindingProblem && (
            <>
              <Metadata
                values={{
                  "Leaf status": leaf.data.status,
                  "Executed strategy version": leaf.data.strategy_version_id,
                  "Executed candidate":
                    leaf.data.candidate_id || "No registered raw candidate FK",
                  "Associated candidate":
                    leaf.data.source_association.candidate_id || "Unavailable",
                  "Candidate association basis":
                    leaf.data.source_association.candidate_basis,
                  "Saved version association basis":
                    leaf.data.source_association.strategy_basis,
                  "Run kind": leaf.data.run_kind,
                  "Dataset snapshot": leaf.data.dataset_version_id,
                  "Universe snapshot":
                    leaf.data.universe_version_id || "Not recorded",
                  "Observation window": leaf.data.window,
                  Split: leaf.data.split,
                }}
              />
              {statusTarget ? (
                <Button asChild variant="outline" size="sm" className="mt-3">
                  <Link {...statusTarget}>Open original run status</Link>
                </Button>
              ) : (
                <p className="notice">
                  The exact final-test status reference is unavailable; no
                  research-run substitute was linked.
                </p>
              )}
              {leaf.data.error && (
                <p className="notice error mt-3">{leaf.data.error}</p>
              )}
              {leaf.data.missing_reasons.map((reason) => (
                <p key={reason} className="text-xs muted mt-2">
                  {reason}
                </p>
              ))}
            </>
          )}
          {!selection.evaluation_id && (
            <p className="notice mt-3">
              No actual leaf Evaluation selected. Run aggregates and template
              descriptions are not execution evidence.
            </p>
          )}
          {selection.trace_status === "not_collected" && (
            <p className="notice mt-3">
              No detailed observations are available for this selection. Any
              registered manifest hash remains pinned, including capture-off or
              unsupported evidence. Internal values and branch outcomes are not
              inferred. Choose another leaf explicitly to inspect different
              evidence.
            </p>
          )}
          {trace.error && <ErrorState error={trace.error} />}
          {leafVerified &&
            (selection.trace_status !== "not_collected" ||
              Boolean(selection.trace_manifest_sha256)) &&
            trace.data &&
            !pinnedTrace && (
              <ErrorState
                error={
                  new Error(
                    "Trace response does not match the pinned source and execution. No observations were displayed.",
                  )
                }
              />
            )}
          {pinnedTrace && (
            <div className="mt-3">
              <Metadata
                values={{
                  "Trace coverage":
                    pinnedTrace.manifest?.coverage || "not_collected",
                  "Capture mode":
                    pinnedTrace.manifest?.capture.mode || "Not collected",
                  "Decisions saved / seen": pinnedTrace.manifest
                    ? `${pinnedTrace.manifest.decisions_saved} / ${pinnedTrace.manifest.decisions_seen}`
                    : "Not collected",
                  "Execution config SHA-256":
                    pinnedTrace.context.binding?.execution_config_sha256,
                  "Dataset SHA-256":
                    pinnedTrace.context.binding?.dataset_sha256,
                  "Universe SHA-256":
                    pinnedTrace.context.binding?.universe_sha256,
                  "Engine dependencies SHA-256":
                    pinnedTrace.context.binding?.engine_dependencies_sha256,
                }}
              />
              {pinnedTrace.context.missing_reason && (
                <p className="notice">{pinnedTrace.context.missing_reason}</p>
              )}
              {pinnedTrace.manifest?.missing_reasons.map((reason) => (
                <p key={reason} className="notice mt-2">
                  {reason}
                </p>
              ))}
              {pinnedTrace.manifest && (
                <p className="notice mt-3">
                  {pinnedTrace.manifest.trust_boundary}
                </p>
              )}
            </div>
          )}
        </Panel>
        <nav aria-label="Research workbench tabs" className="workbench-tabs">
          {workbenchTabs.map((tab) => (
            <Link
              key={tab}
              to="/strategies/$strategyId/research"
              params={{ strategyId }}
              search={{ ...selection, tab }}
              aria-current={activeTab === tab ? "page" : undefined}
            >
              {labels[tab]}
            </Link>
          ))}
        </nav>
        {activeTab === "description" ? (
          <>
            <Panel title="Rules and observation coverage">
              <p className="text-sm mb-3">
                Description coverage: {spec.data.spec.coverage}. Source code,
                parameter declarations and description revisions are distinct
                identities.
              </p>
              {spec.data.spec.missing_reasons.map((reason) => (
                <p key={reason} className="notice mb-3">
                  {reason}
                </p>
              ))}
              {spec.data.spec.template_id ? (
                <RuleGraph
                  spec={spec.data.spec}
                  selectedRule={selectedRule?.rule_id}
                  detail={!bindingProblem ? trustedDetail : undefined}
                  onSelect={selectRule}
                />
              ) : (
                <p className="notice mb-4">
                  No controlled rule graph. Arbitrary code is not reconstructed
                  into a verified strategy; only declared or syntax-inspected
                  rules are shown.
                </p>
              )}
              {selection.rule_id && !selectedRule && (
                <p role="alert" className="notice error">
                  The saved rule is absent from this exact revision. No other
                  rule was substituted.
                </p>
              )}
              {detail.error && <ErrorState error={detail.error} />}
              {detail.data && !trustedDetail && (
                <p role="alert" className="notice error">
                  Returned decision details do not match the saved selection.
                  They are not displayed as actual evidence.
                </p>
              )}
              <RuleCards
                spec={spec.data.spec}
                selectedRule={selectedRule?.rule_id}
                detail={!bindingProblem ? trustedDetail : undefined}
                onSelect={selectRule}
              />
            </Panel>
            <AnchoredSource
              code={currentSource}
              codeSha256={currentHash}
              anchor={selectedRule?.source_ref}
            />
            <DescriptionRevisions
              key={`${source.id}:${spec.data.revision}`}
              owner={owner}
              source={source}
              spec={spec.data}
              onSelect={selectSpec}
            />
          </>
        ) : activeTab === "replay" ? (
          <DecisionReplay
            selection={selection}
            update={update}
            leaf={leaf.data}
            trace={pinnedTrace}
            verified={leafVerified}
            code={currentSource}
            codeSha256={currentHash}
          />
        ) : activeTab === "comparison" ? (
          <MechanismComparison
            owner={owner}
            selection={selection}
            update={update}
            currentSpec={spec.data}
            currentLeaf={leaf.data}
            currentVerified={leafVerified}
          />
        ) : activeTab === "verification" ? (
          <StrategyVerification
            owner={owner}
            selection={selection}
            update={update}
            currentSpec={spec.data}
            currentLeaf={leaf.data}
            currentVerified={leafVerified}
            currentCode={currentSource}
          />
        ) : (
          <Panel title={labels[activeTab]}>
            <p className="notice">
              I6 versioned evidence notes is pending: not implemented in this
              phase. Your exact selection is preserved. No replay, comparison,
              validation result or note has been fabricated.
            </p>
          </Panel>
        )}
      </div>
    </div>
  );
}
