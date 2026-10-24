import { IndependentResults } from "./independent-results";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type {
  IndependentCapability,
  WorkbenchEvaluation,
  WorkbenchSpec,
} from "@/client/types.gen";
import { ErrorState, Loading, Panel } from "@/components/common";
import { ResearchCaptureControls } from "@/components/research-capture-controls";
import { Button } from "@/components/ui/button";
import {
  independentCapabilityOptions,
  createDiagnostic,
  independentStatusOptions,
} from "@/lib/verification-api";
import {
  diagnosticRequestSchema,
  type DiagnosticRequest,
  type IndependentSource,
} from "@/lib/verification-draft";
import {
  diagnosticResultPins,
  independentEvidenceNotice,
  independentStatusProblem,
  sameIndependentSource,
  verificationSource,
} from "@/lib/verification-evidence";
import {
  clearedDiagnosticSelection,
  type WorkbenchSelection,
} from "@/lib/workbench-selection";
import type { ResearchCapture } from "@/lib/research-capture";
import {
  FrozenVerificationInputs,
  IndependentJob,
  IndependentLeafLinks,
} from "./verification-panels";
import {
  VerificationSubmissionState,
  useVerificationSubmission,
} from "./verification-submission";
import { ControlledVerification } from "./controlled-verification";
import { DiagnosticParity } from "./diagnostic-parity";

export type VerificationUpdate = (
  patch: Partial<WorkbenchSelection>,
  replace?: boolean,
) => void;
export function StrategyVerification({
  owner,
  selection,
  update,
  currentSpec,
  currentLeaf,
  currentVerified,
  currentCode,
}: {
  owner: string;
  selection: WorkbenchSelection;
  update: VerificationUpdate;
  currentSpec: WorkbenchSpec;
  currentLeaf?: WorkbenchEvaluation;
  currentVerified: boolean;
  currentCode: string;
}) {
  let source: IndependentSource | null = null,
    sourceProblem: string | null = null;
  try {
    source = verificationSource(
      selection,
      currentSpec,
      currentLeaf,
      currentVerified,
    );
  } catch (error) {
    sourceProblem = (error as Error).message;
  }
  const capability = useQuery(independentCapabilityOptions(owner, source));
  const capabilityProblem =
    capability.data &&
    (!sameIndependentSource(source, capability.data.source) ||
      capability.data.source_code !== currentCode)
      ? "The capability response differs from the exact selected source and physical leaf. It is not displayed or used for dispatch."
      : null;
  return (
    <div className="stack" data-testid="strategy-verification">
      <Panel title="Independent diagnostics and manual controls">
        <p className="notice">{independentEvidenceNotice}</p>
        <p>
          Diagnostics reconstruct the original source. Controls test two or
          three explicitly ordered variants, including a new copied baseline.
          Neither is an optimizer or an automatic causal-validation service.
        </p>
      </Panel>
      {sourceProblem && (
        <p className="notice" role="status">
          {sourceProblem}
        </p>
      )}
      {source && capability.isPending && (
        <Loading text="Admitting frozen source, runtime capability and inherited exposure…" />
      )}
      {capability.error && (
        <ErrorState
          error={capability.error}
          retry={() => void capability.refetch()}
        />
      )}
      {capabilityProblem && <ErrorState error={new Error(capabilityProblem)} />}
      {source && capability.data && !capability.error && !capabilityProblem && (
        <>
          <FrozenVerificationInputs capability={capability.data} />
          <DiagnosticVerification
            key={`diagnostic:${JSON.stringify(source)}`}
            owner={owner}
            selection={selection}
            update={update}
            source={source}
            strategyVersionId={currentSpec.strategy_version_id}
            capability={capability.data}
          />
          <ControlledVerification
            key={`controlled:${JSON.stringify(source)}`}
            owner={owner}
            selection={selection}
            update={update}
            source={source}
            strategyVersionId={currentSpec.strategy_version_id}
            capability={capability.data}
            split={currentLeaf!.split}
          />
        </>
      )}
    </div>
  );
}
function DiagnosticVerification({
  owner,
  selection,
  update,
  source,
  strategyVersionId,
  capability,
}: {
  owner: string;
  selection: WorkbenchSelection;
  update: VerificationUpdate;
  source: IndependentSource;
  strategyVersionId: string;
  capability: IndependentCapability;
}) {
  const [capture, setCapture] = useState<ResearchCapture | undefined>({
    mode: "research",
  });
  const parsed = diagnosticRequestSchema.safeParse({
    source,
    research_capture: capture ?? { mode: "summary" },
    max_runtime_seconds: 300,
  });
  const submission = useVerificationSubmission({
    owner,
    kind: "diagnostic",
    strategyVersionId,
    source,
    body: parsed.success ? parsed.data : null,
    execute: (body, key) => createDiagnostic(body as DiagnosticRequest, key),
    accepted: (response) => {
      update({
        ...clearedDiagnosticSelection(),
        diagnostic_run_id: response.id,
        diagnostic_config_sha256: response.config_sha256,
      });
    },
  });
  const status = useQuery({
    ...independentStatusOptions(owner, selection.diagnostic_run_id),
    enabled: Boolean(selection.diagnostic_run_id),
  });
  const problem = status.data
    ? independentStatusProblem(source, status.data, {
        id: selection.diagnostic_run_id!,
        purpose: "diagnostic",
        config_sha256: selection.diagnostic_config_sha256,
      }) || diagnosticResultPins(selection, status.data.result_pin)
    : null;
  const trusted =
    status.data && !status.error && !problem ? status.data : undefined;
  useEffect(() => {
    if (!trusted) return;
    const pin = trusted.result_pin;
    const patch: Partial<WorkbenchSelection> = {};
    if (!selection.diagnostic_config_sha256)
      patch.diagnostic_config_sha256 = trusted.config_sha256;
    if (pin && !selection.diagnostic_evaluation_id)
      Object.assign(patch, {
        diagnostic_evaluation_id: pin.evaluation_id,
        diagnostic_execution_attempt_id: pin.execution_attempt_id,
        diagnostic_result_manifest_sha256: pin.result_manifest_sha256,
        diagnostic_trace_manifest_sha256:
          pin.trace_manifest_sha256 ?? undefined,
      });
    if (Object.keys(patch).length) update(patch, true);
  }, [
    trusted,
    selection.diagnostic_config_sha256,
    selection.diagnostic_evaluation_id,
    update,
  ]);
  return (
    <>
      <Panel title="Create independent diagnostic replay">
        {!submission.draft && (
          <ResearchCaptureControls
            value={capture}
            onChange={setCapture}
            disabled={submission.busy || !capability.can_execute}
            prefix="diagnostic_capture"
            portable={capability.evaluation_mode === "portable"}
          />
        )}
        <p className="text-xs muted">
          The exact frozen source code, parameters, snapshots, original start
          and warmup are not editable. This request has a 300-second execution
          budget. Original evidence and result hashes remain unchanged.
        </p>
        <VerificationSubmissionState state={submission} kind="diagnostic" />
        {!parsed.success && !submission.draft && (
          <ErrorState
            error={
              new Error(
                parsed.error.issues
                  .map((issue) => `${issue.path.join(".")}: ${issue.message}`)
                  .join("; "),
              )
            }
          />
        )}
        <Button
          className="mt-3"
          disabled={
            !capability.can_execute ||
            submission.busy ||
            submission.unreadable ||
            !submission.sameSource ||
            (!submission.draft && !parsed.success)
          }
          onClick={() => void submission.submit()}
        >
          {submission.busy
            ? "Creating independent diagnostic…"
            : submission.draft
              ? "Retry same diagnostic submission"
              : "Create independent diagnostic replay"}
        </Button>
      </Panel>
      {selection.diagnostic_run_id && (
        <>
          {status.isPending && (
            <Loading text="Reading durable independent diagnostic status…" />
          )}
          {status.error && (
            <ErrorState
              error={status.error}
              retry={() => void status.refetch()}
            />
          )}
          {problem && <ErrorState error={new Error(problem)} />}
          {trusted && (
            <>
              <IndependentJob key={trusted.id} owner={owner} job={trusted} />
              {trusted.result_pin && (
                <DiagnosticParity
                  owner={owner}
                  source={source}
                  job={trusted}
                  selection={selection}
                  update={update}
                />
              )}
              {trusted.result_pin && (
                <IndependentResults
                  owner={owner}
                  job={trusted}
                  pin={trusted.result_pin}
                />
              )}
              {trusted.result_pin && (
                <Panel title="Independent execution result">
                  <p className="notice" data-testid="diagnostic-evidence-class">
                    New execution observations are not original-run facts. Use
                    the exact semantic report above to distinguish verified
                    diagnostics, mismatches and not-verifiable results.
                  </p>
                  <IndependentLeafLinks
                    pin={trusted.result_pin}
                    label="Diagnostic execution observation"
                  />
                </Panel>
              )}
            </>
          )}
        </>
      )}
    </>
  );
}
