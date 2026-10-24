import { useEffect, useRef, useState } from "react";
import { AnchoredSource } from "./anchored-source";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import type {
  IndependentCapability,
  IndependentEvaluationDetail,
  IndependentResultPin,
  WorkbenchSpec,
} from "@/client/types.gen";
import { ErrorState, Loading, Panel } from "@/components/common";
import { JsonDetails, Metadata } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { ExposurePanel } from "@/features/final-tests/exposure";
import { queryClient } from "@/lib/api";
import { useRunEvents } from "@/lib/use-run-events";
import { asRecord, valueText } from "@/lib/data-utils";
import { sideFromEvidence } from "@/lib/comparison-evidence";
import {
  workbenchEvaluationOptions,
  workbenchSpecOptions,
} from "@/lib/workbench-api";
import { leafBindingProblem, leafSelection } from "@/lib/workbench-evidence";
import {
  comparisonSidePatch,
  type WorkbenchSelection,
} from "@/lib/workbench-selection";
import { changeIndependent } from "@/lib/verification-api";
import {
  clearVerificationAction,
  loadVerificationAction,
  storeVerificationAction,
  verificationActionResolved,
  type VerificationAction,
} from "@/lib/verification-draft";
import {
  fieldConditions,
  independentTerminal,
} from "@/lib/verification-evidence";

export function FrozenVerificationInputs({
  capability,
}: {
  capability: IndependentCapability;
}) {
  const conditions = capability.fixed_conditions;
  return (
    <>
      <Panel title="Original evidence and frozen independent inputs">
        <p className="notice">
          No optimizer, model, data provider or broker is called. The exact
          source is copied into a new immutable version; the original code is
          never instrumented in place.
        </p>
        <Metadata
          values={{
            "Source run": capability.source_run_id,
            "Original leaf Evaluation": capability.source.evaluation_id,
            "Original physical attempt": capability.source.execution_attempt_id,
            "Original result SHA-256": capability.source.result_manifest_sha256,
            "Original trace SHA-256":
              capability.source.trace_manifest_sha256 ??
              "Historical absence, not a captured empty trace",
            "Original code SHA-256": capability.source.code_sha256,
            "Exact source description revision":
              capability.source.spec_revision,
            "Exact source description SHA-256": capability.source.spec_sha256,
            "Original configuration SHA-256": capability.original_config_sha256,
            "Execution mode": capability.evaluation_mode,
            "Fixed-condition protocol SHA-256": capability.protocol_sha256,
          }}
        />
        <p className="notice mt-3" data-testid="reconstruction-window-notice">
          State reconstruction starts from the original Evaluation start with
          its original warmup and runs through the original scored end. The
          observation window below only filters saved detail. It never changes
          computation, the candidate pool, state reconstruction or holdout
          admission. An optimizer checkpoint is not a complete engine-state
          checkpoint.
        </p>
        <FixedConditionFields conditions={conditions} />
        <details className="mt-4">
          <summary>Exact frozen original source</summary>
          <AnchoredSource
            code={capability.source_code}
            codeSha256={capability.source.code_sha256}
          />
        </details>
        <JsonDetails
          title="Exact frozen original business parameters"
          value={capability.source_parameters}
        />
        <JsonDetails
          title="Exact frozen original configuration"
          value={capability.original_config}
        />
        <JsonDetails
          title="Replay, RNG, state and observation prerequisites"
          value={capability.parity_prerequisites}
        />
        {capability.unavailable_reasons.length > 0 && (
          <section
            aria-label="Independent execution unavailable reasons"
            className="notice error"
          >
            <p>Independent execution unavailable:</p>
            <ul>
              {capability.unavailable_reasons.map((reason, index) => (
                <li key={index}>{reason}</li>
              ))}
            </ul>
          </section>
        )}
        <p className="text-xs muted">
          Missing original RNG state, replay state, environment or observation
          coverage stays explicitly unavailable or not verifiable. Equal returns
          cannot replace these prerequisites. Unseeded generators, OS entropy
          and self-reseeding code are not silently certified.
        </p>
      </Panel>
      <ExposurePanel
        snapshot={capability.exposure_snapshot}
        title="Inherited family and owner-wide holdout exposure"
      />
    </>
  );
}
export function FixedConditionFields({
  conditions,
}: {
  conditions: Record<string, unknown>;
}) {
  return (
    <section className="mt-3" aria-label="Fixed protocol field conditions">
      <h3>Fixed protocol field conditions</h3>
      <dl className="data-metadata" data-testid="fixed-protocol-fields">
        {fieldConditions(conditions).map(({ field, value }) => (
          <div key={field}>
            <dt>{field}</dt>
            <dd>{valueText(value)}</dd>
          </div>
        ))}
      </dl>
      <p className="text-xs muted">
        Only intended source code and business-parameter values may differ
        across arms. Data, universe, scored window, warmup, cash/holdings
        initialization, state policy, fees, fill semantics, contract
        declarations, lookback and dependencies remain fixed. Historical
        trajectories may differ and are not a fixed-protocol field.
      </p>
    </section>
  );
}
export function IndependentJob({
  owner,
  job,
}: {
  owner: string;
  job: IndependentEvaluationDetail;
}) {
  const [initial] = useState(() => {
    try {
      return {
        pending: loadVerificationAction(owner, job.id),
        error: null as unknown,
      };
    } catch (error) {
      return { pending: null, error };
    }
  });
  const [pending, setPending] = useState<VerificationAction | null>(
    initial.pending,
  );
  const [error, setError] = useState<unknown>(initial.error);
  const [busy, setBusy] = useState(false);
  const lock = useRef(false),
    mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const blocked =
    Boolean(initial.error) ||
    Boolean(pending && pending.config_sha256 !== job.config_sha256);
  useEffect(() => {
    if (pending && verificationActionResolved(pending, job)) {
      try {
        clearVerificationAction(owner, job.id, pending.key);
        setPending(null);
        setError(null);
      } catch (failure) {
        setError(failure);
      }
    }
  }, [owner, job, pending]);
  async function act(action: "cancel" | "retry") {
    if (lock.current || blocked || (pending && pending.action !== action))
      return;
    lock.current = true;
    setBusy(true);
    setError(null);
    try {
      const intent = pending || {
        action,
        key: crypto.randomUUID(),
        generation: job.generation,
        config_sha256: job.config_sha256,
      };
      storeVerificationAction(owner, job.id, intent);
      setPending(intent);
      const response = await changeIndependent(
        job.id,
        intent.action,
        intent.key,
      );
      if (
        response.id !== job.id ||
        response.config_sha256 !== job.config_sha256 ||
        response.request_sha256 !== job.request_sha256
      )
        throw new Error(
          "The action response differs from the immutable job. Refresh durable status; the original action is retained.",
        );
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["independent-evaluation", owner, job.id],
        }),
        queryClient.invalidateQueries({
          queryKey: ["controlled-experiment", owner],
        }),
      ]);
      if (verificationActionResolved(intent, response)) {
        clearVerificationAction(owner, job.id, intent.key);
        if (mounted.current) setPending(null);
      }
    } catch (failure) {
      if (mounted.current) setError(failure);
    } finally {
      lock.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  const terminal = independentTerminal.has(job.status);
  const retryable =
    ["failed", "cancelled", "interrupted"].includes(job.status) &&
    job.attempts.every((attempt) => attempt.sandbox_stopped);
  return (
    <Panel title={`Independent ${job.purpose} job`}>
      <p data-testid={`independent-status-${job.id}`} role="status">
        {job.status}
      </p>
      <Metadata
        values={{
          "Immutable run": job.id,
          "Independent job": job.independent_id,
          Purpose: job.purpose,
          Generation: job.generation,
          "Request SHA-256": job.request_sha256,
          "Independent configuration SHA-256": job.config_sha256,
          "Active physical attempt": job.active_attempt_id,
          "New leaf Evaluation": job.evaluation_id,
          "Result ready": job.result_ready,
          "State reconstruction [start, end)": job.config.reconstruction_window,
          "Original warmup": job.config.source_config.warmup,
          "Saved observation window":
            job.config.research_capture?.observation_window ??
            "All reconstruction dates",
          "State policy": job.config.state_policy,
        }}
      />
      {job.error && (
        <p className="notice error">
          Failed or interrupted: {job.error}. This is not a zero-return
          successful result.
        </p>
      )}
      {pending && (
        <p className="notice">
          The {pending.action} outcome is pending or uncertain. Only the same
          retained action may be retried. Database status, not a timeout,
          establishes its outcome.
        </p>
      )}
      <div className="flex flex-wrap gap-3 mt-3">
        {!terminal && (
          <Button
            variant="outline"
            disabled={
              busy || blocked || Boolean(pending && pending.action !== "cancel")
            }
            onClick={() => void act("cancel")}
          >
            Cancel independent run
          </Button>
        )}
        {["failed", "cancelled", "interrupted"].includes(job.status) && (
          <Button
            variant="outline"
            disabled={
              !retryable ||
              busy ||
              blocked ||
              Boolean(pending && pending.action !== "retry")
            }
            onClick={() => void act("retry")}
          >
            Retry independent run from original start
          </Button>
        )}
        {pending && (
          <Button
            variant="outline"
            disabled={busy || blocked}
            onClick={() => void act(pending.action)}
          >
            Retry same independent {pending.action} action
          </Button>
        )}
        <Button
          variant="outline"
          onClick={() =>
            void queryClient.invalidateQueries({
              queryKey: ["independent-evaluation", owner, job.id],
            })
          }
        >
          Refresh independent status
        </Button>
      </div>
      <p className="text-xs muted mt-3">
        Retries require confirmed prior sandbox stop and rerun the complete
        original start plus warmup. No optimizer resume or partial-state
        restoration is implied.
      </p>
      {error != null && <ErrorState error={error} />}
      <details className="mt-3">
        <summary>Persisted independent attempts</summary>
        {job.attempts.map((attempt) => (
          <Metadata
            key={attempt.id}
            values={{
              ID: attempt.id,
              Status: attempt.status,
              "Sandbox stopped": attempt.sandbox_stopped,
            }}
          />
        ))}
      </details>
      <IndependentProgress runId={job.id} terminal={terminal} />
    </Panel>
  );
}
function IndependentProgress({
  runId,
  terminal,
}: {
  runId: string;
  terminal: boolean;
}) {
  const events = useRunEvents(runId, terminal);
  return (
    <details className="mt-3">
      <summary>Independent progress notifications</summary>
      <p className="text-xs muted">
        Progress-only SSE, with durable database polling as the authoritative
        fallback. Results are separately admitted, not inferred from
        notifications.
      </p>
      {events.error && (
        <p className="notice">
          Progress notifications are unavailable. Durable status polling
          continues.
        </p>
      )}
      {events.data?.map((event) => (
        <p key={event.seq} className="text-xs">
          {event.seq} · {event.type} · {event.occurred_at}
        </p>
      ))}
    </details>
  );
}
export function independentLeafProblem(
  pin: IndependentResultPin,
  leaf: import("@/client/types.gen").WorkbenchEvaluation,
  spec: WorkbenchSpec,
) {
  return pin.source_id !== spec.strategy_version_id ||
    spec.resource_kind !== "strategy_version" ||
    spec.resource_id !== pin.source_id ||
    spec.code_sha256 !== pin.code_sha256 ||
    leaf.evaluation_id !== pin.evaluation_id ||
    leaf.execution_attempt_id !== pin.execution_attempt_id ||
    leaf.result_manifest_sha256 !== pin.result_manifest_sha256 ||
    leaf.trace_manifest_sha256 !== pin.trace_manifest_sha256 ||
    leaf.code_sha256 !== pin.code_sha256
    ? "The independent source, physical leaf or artifact no longer matches its immutable result pin. No link or metric was substituted."
    : leafBindingProblem({ code_sha256: pin.code_sha256 }, pin.source_id, leaf);
}
export function IndependentLeafLinks({
  pin,
  label = "Independent observation",
  comparisonBaseline,
}: {
  pin: IndependentResultPin;
  label?: string;
  comparisonBaseline?: IndependentResultPin;
}) {
  const spec = useQuery(
    workbenchSpecOptions({ kind: "strategy_version", id: pin.source_id }),
  );
  const leaf = useQuery(workbenchEvaluationOptions(pin.evaluation_id));
  const problem =
    spec.data && leaf.data
      ? independentLeafProblem(pin, leaf.data, spec.data)
      : null;
  if (spec.error || leaf.error)
    return <ErrorState error={spec.error || leaf.error} />;
  if (problem) return <ErrorState error={new Error(problem)} />;
  if (!spec.data || !leaf.data)
    return (
      <Loading text="Admitting exact independent source and physical result…" />
    );
  const selection = leafSelection(
    {
      tab: "replay",
      code_sha256: pin.code_sha256,
      spec_revision: spec.data.revision,
      spec_sha256: spec.data.spec_sha256,
    },
    leaf.data,
  );
  return (
    <section aria-label={`${label} exact evidence`}>
      <Metadata
        values={{
          "Evidence class": label,
          "Exact new source": pin.source_id,
          "Exact leaf": pin.evaluation_id,
          "Exact physical attempt": pin.execution_attempt_id,
          "Result SHA-256": pin.result_manifest_sha256,
          "Bound execution configuration SHA-256": pin.execution_config_sha256,
          "Trace SHA-256": pin.trace_manifest_sha256 ?? "Not collected",
        }}
      />
      <Button asChild variant="outline" className="mt-3">
        <Link
          to="/strategies/$strategyId/research"
          params={{ strategyId: pin.source_id }}
          search={selection}
        >
          Open {label.toLowerCase()} replay
        </Link>
      </Button>
      {comparisonBaseline && (
        <ControlledComparisonLink
          baseline={comparisonBaseline}
          variant={pin}
          variantSpec={spec.data}
          variantLeaf={leaf.data}
        />
      )}
    </section>
  );
}
function ControlledComparisonLink({
  baseline,
  variant,
  variantSpec,
  variantLeaf,
}: {
  baseline: IndependentResultPin;
  variant: IndependentResultPin;
  variantSpec: WorkbenchSpec;
  variantLeaf: import("@/client/types.gen").WorkbenchEvaluation;
}) {
  const spec = useQuery(
    workbenchSpecOptions({ kind: "strategy_version", id: baseline.source_id }),
  );
  const leaf = useQuery(workbenchEvaluationOptions(baseline.evaluation_id));
  if (spec.error || leaf.error)
    return <ErrorState error={spec.error || leaf.error} />;
  if (!spec.data || !leaf.data)
    return <Loading text="Admitting copied baseline for exact comparison…" />;
  const problem = independentLeafProblem(baseline, leaf.data, spec.data);
  if (problem) return <ErrorState error={new Error(problem)} />;
  const search: WorkbenchSelection = {
    tab: "comparison",
    ...comparisonSidePatch("left", sideFromEvidence(spec.data, leaf.data)),
    ...comparisonSidePatch("right", sideFromEvidence(variantSpec, variantLeaf)),
  };
  return (
    <Button asChild variant="outline" className="mt-3">
      <Link
        to="/strategies/$strategyId/research"
        params={{ strategyId: variant.source_id }}
        search={search}
      >
        Compare copied baseline with this arm
      </Link>
    </Button>
  );
}

export function prerequisiteRows(value: Record<string, unknown>) {
  return fieldConditions(asRecord(value));
}
