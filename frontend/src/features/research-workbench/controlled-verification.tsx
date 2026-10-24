import { IndependentResults } from "./independent-results";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type {
  ControlledExperimentPublic,
  IndependentCapability,
} from "@/client/types.gen";
import { CodeDiff, CodeEditor } from "@/components/code-editor";
import { ErrorState, Loading, Panel } from "@/components/common";
import { Field, JsonDetails, Metadata } from "@/components/data-common";
import { ResearchCaptureControls } from "@/components/research-capture-controls";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ExposurePanel } from "@/features/final-tests/exposure";
import { queryClient } from "@/lib/api";
import {
  controlledExperimentOptions,
  createControlled,
  independentStatusOptions,
} from "@/lib/verification-api";
import {
  controlledRequestSchema,
  type ControlledRequest,
  type IndependentSource,
} from "@/lib/verification-draft";
import {
  controlledStatusProblem,
  independentStatusProblem,
  previewArmChanges,
} from "@/lib/verification-evidence";
import {
  clearedControlledArmSelection,
  clearedControlledSelection,
  type WorkbenchSelection,
} from "@/lib/workbench-selection";
import type { ResearchCapture } from "@/lib/research-capture";
import {
  FixedConditionFields,
  IndependentJob,
  IndependentLeafLinks,
} from "./verification-panels";
import {
  VerificationSubmissionState,
  useVerificationSubmission,
} from "./verification-submission";
import type { VerificationUpdate } from "./verification";
type ArmDraft = {
  name: string;
  code: string;
  parameterText: string;
  planned_change: string;
};
export function ControlledVerification({
  owner,
  selection,
  update,
  source,
  strategyVersionId,
  capability,
  split,
}: {
  owner: string;
  selection: WorkbenchSelection;
  update: VerificationUpdate;
  source: IndependentSource;
  strategyVersionId: string;
  capability: IndependentCapability;
  split: string;
}) {
  const [name, setName] = useState(""),
    [hypothesis, setHypothesis] = useState(""),
    [plannedChange, setPlannedChange] = useState("");
  const [capture, setCapture] = useState<ResearchCapture | undefined>({
    mode: "research",
  });
  const copiedArm = (name: string): ArmDraft => ({
    name,
    code: capability.source_code,
    parameterText: JSON.stringify(capability.source_parameters, null, 2),
    planned_change: "",
  });
  const [arms, setArms] = useState<ArmDraft[]>(() => [
    copiedArm("Copied baseline"),
    copiedArm("Variant 1"),
  ]);
  const [editing, setEditing] = useState(1);
  let body: ControlledRequest | null = null,
    formProblem: string | null = null;
  try {
    body = controlledRequestSchema.parse({
      name,
      hypothesis,
      planned_change: plannedChange,
      source,
      arms: arms.map((arm) => ({
        name: arm.name,
        code: arm.code,
        parameters: JSON.parse(arm.parameterText),
        planned_change: arm.planned_change || null,
      })),
      research_capture: capture ?? { mode: "summary" },
      max_runtime_seconds: 300,
    });
    if (
      body.arms[0].code !== capability.source_code ||
      JSON.stringify(body.arms[0].parameters) !==
        JSON.stringify(capability.source_parameters)
    )
      throw new Error("The first arm must be an exact copied baseline.");
  } catch (error) {
    formProblem =
      error instanceof SyntaxError
        ? "Each arm's business parameters must be valid JSON. No altered declaration or manifest is accepted."
        : error instanceof Error
          ? error.message
          : "Complete the manual control protocol.";
  }
  const submission = useVerificationSubmission({
    owner,
    kind: "controlled",
    strategyVersionId,
    source,
    body,
    execute: (request, key) =>
      createControlled(request as ControlledRequest, key),
    accepted: (response) => {
      queryClient.setQueryData(
        ["controlled-experiment", owner, response.id],
        response,
      );
      update({
        ...clearedControlledSelection(),
        controlled_experiment_id: response.id,
        controlled_request_sha256: response.request_sha256,
        controlled_protocol_sha256: response.protocol_sha256,
      });
    },
  });
  const interactive = split === "train" || split === "validation";
  const locked =
    submission.busy ||
    Boolean(submission.draft) ||
    !interactive ||
    !capability.can_execute ||
    !capability.can_control;
  const current = arms[editing];
  function edit(patch: Partial<ArmDraft>) {
    if (editing === 0) return;
    setArms((previous) =>
      previous.map((arm, index) =>
        index === editing ? { ...arm, ...patch } : arm,
      ),
    );
  }
  let preview: ReturnType<typeof previewArmChanges> | null = null;
  try {
    preview = previewArmChanges(
      capability.source_code,
      capability.source_parameters,
      current.code,
      JSON.parse(current.parameterText),
    );
  } catch {
    /* Invalid editable JSON is displayed as an error, never an accepted variant. */
  }
  const status = useQuery(
    controlledExperimentOptions(owner, selection.controlled_experiment_id),
  );
  const problem = status.data
    ? controlledStatusProblem(source, selection, status.data)
    : null;
  const trusted =
    status.data && !status.error && !problem ? status.data : undefined;
  useEffect(() => {
    if (
      trusted &&
      (!selection.controlled_request_sha256 ||
        !selection.controlled_protocol_sha256)
    )
      update(
        {
          controlled_request_sha256: trusted.request_sha256,
          controlled_protocol_sha256: trusted.protocol_sha256,
        },
        true,
      );
  }, [
    trusted,
    selection.controlled_request_sha256,
    selection.controlled_protocol_sha256,
    update,
  ]);
  return (
    <>
      <Panel title="Create manual two- or three-arm controlled experiment">
        <p className="notice">
          Development/train or validation only. Every arm creates its own
          immutable StrategyVersion, independent ResearchRun and physical
          Evaluation. The first is an exact copied baseline, not the historical
          original run. Failed or cancelled arms remain failed or cancelled,
          never zero-return successes.
        </p>
        {capability.control_unavailable_reasons.length > 0 && (
          <section
            className="notice error"
            aria-label="Controlled execution unavailable reasons"
          >
            <p>Controlled execution unavailable:</p>
            <ul>
              {capability.control_unavailable_reasons.map((reason, index) => (
                <li key={index}>{reason}</li>
              ))}
            </ul>
          </section>
        )}
        {!interactive && (
          <p className="notice error">
            Controls cannot execute an interactive holdout. Select a
            development/train or validation leaf. Copying a run or experiment
            never resets family or owner-wide holdout exposure.
          </p>
        )}
        {!submission.draft && (
          <fieldset disabled={locked} className="stack">
            <Field id="controlled-name" label="Controlled experiment name">
              <Input
                id="controlled-name"
                value={name}
                maxLength={255}
                onChange={(event) => setName(event.target.value)}
              />
            </Field>
            <Field id="controlled-hypothesis" label="Hypothesis">
              <textarea
                id="controlled-hypothesis"
                rows={3}
                value={hypothesis}
                maxLength={4000}
                onChange={(event) => setHypothesis(event.target.value)}
              />
            </Field>
            <Field
              id="controlled-planned-change"
              label="Planned change and isolated mechanism"
            >
              <textarea
                id="controlled-planned-change"
                rows={3}
                value={plannedChange}
                maxLength={4000}
                onChange={(event) => setPlannedChange(event.target.value)}
              />
            </Field>
            <Metadata
              values={{
                "Fixed protocol SHA-256": capability.protocol_sha256,
                "Scored split": split,
                "Execution budget per arm": "300 seconds",
                "Business parameters copied from source":
                  capability.source_parameters,
              }}
            />
            <div
              className="flex flex-wrap gap-2"
              aria-label="Ordered control variants"
            >
              {arms.map((arm, index) => (
                <Button
                  type="button"
                  variant={editing === index ? "default" : "outline"}
                  key={index}
                  aria-pressed={editing === index}
                  onClick={() => setEditing(index)}
                >
                  {index + 1}. {arm.name || "Unnamed arm"}
                </Button>
              ))}
              {arms.length === 2 && (
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    setArms((previous) => [
                      ...previous,
                      copiedArm("Variant 2"),
                    ]);
                    setEditing(2);
                  }}
                >
                  Add third arm
                </Button>
              )}
              {arms.length === 3 && (
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    setArms((previous) => previous.slice(0, 2));
                    setEditing(Math.min(editing, 1));
                  }}
                >
                  Remove third draft arm
                </Button>
              )}
            </div>
            {editing === 0 ? (
              <p className="notice">
                Copied baseline is locked to the exact source code and business
                parameters. It is newly executed with the shared protocol, not
                reused as a fabricated baseline result.
              </p>
            ) : (
              <Field id="controlled-arm-name" label="Variant name">
                <Input
                  id="controlled-arm-name"
                  value={current.name}
                  maxLength={120}
                  onChange={(event) => edit({ name: event.target.value })}
                />
              </Field>
            )}
            <Field
              id="controlled-arm-change"
              label="This arm's planned mechanism change"
            >
              <textarea
                id="controlled-arm-change"
                rows={2}
                disabled={editing === 0}
                value={current.planned_change}
                onChange={(event) =>
                  edit({ planned_change: event.target.value })
                }
              />
            </Field>
            <Field
              id="controlled-arm-parameters"
              label="Business parameter values (JSON)"
              help="Only declared business values may change. State policy, lookback, warmup, dependencies and contract declarations remain server-frozen."
            >
              <textarea
                id="controlled-arm-parameters"
                rows={7}
                disabled={editing === 0}
                value={current.parameterText}
                onChange={(event) =>
                  edit({ parameterText: event.target.value })
                }
              />
            </Field>
            <CodeEditor
              code={current.code}
              onChange={(code) => edit({ code })}
              disabled={locked || editing === 0}
              label="Controlled variant Python source"
            />
            {preview && (
              <Metadata
                values={{
                  "Proposed changed fields": preview.fields.length
                    ? preview.fields
                    : "No change",
                  "Interpretation before execution":
                    preview.interpretation.replaceAll("_", " "),
                }}
              />
            )}
            <p className="notice">
              Multiple changes, arbitrary code edits and unrecognized patches
              are not individually validated. A single business-parameter change
              is still not causal proof. Review the full patch and fixed
              protocol before executing.
            </p>
            <CodeDiff
              original={capability.source_code}
              modified={current.code}
              label="Controlled baseline and variant source diff"
            />
            <ResearchCaptureControls
              value={capture}
              onChange={setCapture}
              disabled={locked}
              prefix="controlled_capture"
              portable={capability.evaluation_mode === "portable"}
            />
          </fieldset>
        )}
        <VerificationSubmissionState state={submission} kind="controlled" />
        {formProblem && !submission.draft && (
          <details className="mt-3">
            <summary>
              Complete required control fields before submission
            </summary>
            <p role="status">{formProblem}</p>
          </details>
        )}
        <Button
          className="mt-3"
          disabled={
            !interactive ||
            !capability.can_execute ||
            !capability.can_control ||
            submission.busy ||
            submission.unreadable ||
            !submission.sameSource ||
            (!submission.draft && !body)
          }
          onClick={() => void submission.submit()}
        >
          {submission.busy
            ? "Creating immutable controlled experiment…"
            : submission.draft
              ? "Retry same controlled submission"
              : `Create ${arms.length}-arm controlled experiment`}
        </Button>
      </Panel>
      {selection.controlled_experiment_id && (
        <>
          {status.isPending && (
            <Loading text="Reading immutable controlled protocol and ordered arms…" />
          )}
          {status.error && (
            <ErrorState
              error={status.error}
              retry={() => void status.refetch()}
            />
          )}
          {problem && <ErrorState error={new Error(problem)} />}
          {trusted && (
            <ControlledResult
              owner={owner}
              source={source}
              result={trusted}
              selection={selection}
              update={update}
            />
          )}
        </>
      )}
    </>
  );
}
function ControlledResult({
  owner,
  source,
  result,
  selection,
  update,
}: {
  owner: string;
  source: IndependentSource;
  result: ControlledExperimentPublic;
  selection: WorkbenchSelection;
  update: VerificationUpdate;
}) {
  const arm =
    selection.controlled_arm === undefined
      ? undefined
      : result.arms[selection.controlled_arm];
  const status = useQuery({
    ...independentStatusOptions(owner, arm?.run_id),
    enabled: Boolean(arm),
  });
  const problem =
    status.data && arm
      ? independentStatusProblem(source, status.data, {
          id: arm.run_id,
          purpose: "controlled",
          result_pin: arm.result_pin,
        })
      : null;
  const trusted =
    status.data && !status.error && !problem ? status.data : undefined;
  useEffect(() => {
    if (arm?.result_pin && !selection.controlled_arm_evaluation_id)
      update(
        {
          controlled_arm_evaluation_id: arm.result_pin.evaluation_id,
          controlled_arm_execution_attempt_id:
            arm.result_pin.execution_attempt_id,
          controlled_arm_result_manifest_sha256:
            arm.result_pin.result_manifest_sha256,
          controlled_arm_trace_manifest_sha256:
            arm.result_pin.trace_manifest_sha256 ?? undefined,
        },
        true,
      );
  }, [arm, selection.controlled_arm_evaluation_id, update]);
  return (
    <>
      <Panel title="Immutable controlled protocol and all outcomes">
        <Metadata
          values={{
            "Controlled experiment": result.id,
            "Request SHA-256": result.request_sha256,
            "Protocol SHA-256": result.protocol_sha256,
            "Child experiment": result.experiment_id,
            "Child-owned split version": result.split_version_id,
            "Parent experiment": result.parent_experiment_id,
            "Root research family": result.root_experiment_id,
            Hypothesis: result.hypothesis,
            "Planned isolated mechanism": result.planned_change,
          }}
        />
        <FixedConditionFields conditions={result.protocol} />
        <ol className="stack mt-3" data-testid="controlled-arms">
          {result.arms.map((item) => (
            <li key={item.run_id} className="rule-card">
              <h3>
                {item.ordinal + 1}. {item.name}
              </h3>
              <Metadata
                values={{
                  Status: item.status,
                  Interpretation: item.interpretation.replaceAll("_", " "),
                  "Changed fields": item.changed_fields.length
                    ? item.changed_fields
                    : "None",
                  "Immutable strategy version": item.strategy_version_id,
                  "Code SHA-256": item.code_sha256,
                  "Independent run": item.run_id,
                  "Physical leaf": item.evaluation_id ?? "Not produced",
                  "Business parameters": item.parameters,
                  "Planned mechanism change":
                    item.planned_change ?? "Not separately specified",
                }}
              />
              {item.error && <p className="notice error">{item.error}</p>}
              {["failed", "cancelled", "interrupted"].includes(item.status) && (
                <p className="notice error">
                  No successful return is assigned to this {item.status} arm. It
                  is retained, not excluded or converted to zero.
                </p>
              )}
              <Button
                variant="outline"
                className="mt-3"
                aria-pressed={arm?.run_id === item.run_id}
                onClick={() =>
                  update({
                    ...clearedControlledArmSelection(),
                    controlled_arm: item.ordinal,
                    controlled_arm_run_id: item.run_id,
                    controlled_request_sha256: result.request_sha256,
                    controlled_protocol_sha256: result.protocol_sha256,
                    ...(item.result_pin
                      ? {
                          controlled_arm_evaluation_id:
                            item.result_pin.evaluation_id,
                          controlled_arm_execution_attempt_id:
                            item.result_pin.execution_attempt_id,
                          controlled_arm_result_manifest_sha256:
                            item.result_pin.result_manifest_sha256,
                          controlled_arm_trace_manifest_sha256:
                            item.result_pin.trace_manifest_sha256 ?? undefined,
                        }
                      : {}),
                  })
                }
              >
                Inspect arm {item.ordinal + 1}
              </Button>
            </li>
          ))}
        </ol>
        <JsonDetails
          title="Exact immutable control creation request"
          value={result.request}
        />
      </Panel>
      <ExposurePanel
        snapshot={result.exposure_snapshot}
        title="Inherited experiment-family and owner-wide exposure after controls"
      />
      {arm && (
        <>
          {status.isPending && (
            <Loading text="Reading selected arm's durable status…" />
          )}
          {status.error && <ErrorState error={status.error} />}
          {problem && <ErrorState error={new Error(problem)} />}
          {trusted && (
            <>
              <IndependentJob key={trusted.id} owner={owner} job={trusted} />
              {trusted.result_pin && (
                <IndependentResults
                  owner={owner}
                  job={trusted}
                  pin={trusted.result_pin}
                />
              )}
              {trusted.result_pin && (
                <Panel title={`Arm ${arm.ordinal + 1} actual observations`}>
                  <IndependentLeafLinks
                    pin={trusted.result_pin}
                    label={`Arm ${arm.ordinal + 1} experiment result`}
                    comparisonBaseline={
                      arm.ordinal !== 0 && result.arms[0].status === "succeeded"
                        ? (result.arms[0].result_pin ?? undefined)
                        : undefined
                    }
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
