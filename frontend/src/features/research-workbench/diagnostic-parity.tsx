import { useEffect } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import type {
  DiagnosticParityView,
  IndependentEvaluationPublic,
} from "@/client/types.gen";
import { ErrorState, Loading, Panel } from "@/components/common";
import { JsonDetails, Metadata, Pagination } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import {
  diagnosticParityEventsOptions,
  diagnosticParityOptions,
} from "@/lib/verification-api";
import {
  diagnosticEvidenceClass,
  diagnosticParityProblem,
  parityPageMatches,
  parityReplayTarget,
} from "@/lib/diagnostic-parity-evidence";
import type { IndependentSource } from "@/lib/verification-draft";
import type { WorkbenchSelection } from "@/lib/workbench-selection";
import type { VerificationUpdate } from "./verification";

export function DiagnosticParity({
  owner,
  source,
  job,
  selection,
  update,
}: {
  owner: string;
  source: IndependentSource;
  job: IndependentEvaluationPublic;
  selection: WorkbenchSelection;
  update: VerificationUpdate;
}) {
  const query = useQuery(
    diagnosticParityOptions(owner, job.id, selection.diagnostic_report_sha256),
  );
  const problem = query.data
    ? diagnosticParityProblem(source, job, selection, query.data)
    : null;
  const trusted =
    query.data && !query.error && !problem ? query.data : undefined;
  useEffect(() => {
    if (trusted && !selection.diagnostic_report_sha256)
      update(
        {
          diagnostic_config_sha256: job.config_sha256,
          diagnostic_report_sha256: trusted.report_sha256,
        },
        true,
      );
  }, [trusted, selection.diagnostic_report_sha256, job.config_sha256, update]);
  return (
    <section aria-label="Diagnostic parity admission" className="stack">
      {query.isPending && (
        <Loading text="Admitting original and replay evidence for semantic parity…" />
      )}
      {query.error && (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      )}
      {!query.error && problem && <ErrorState error={new Error(problem)} />}
      {trusted && (
        <>
          <DiagnosticParitySummary report={trusted} />
          <DiagnosticParityEvents
            owner={owner}
            runId={job.id}
            report={trusted}
            selection={selection}
            update={update}
          />
        </>
      )}
    </section>
  );
}
export function DiagnosticParitySummary({
  report,
}: {
  report: DiagnosticParityView;
}) {
  return (
    <Panel title="Immutable semantic diagnostic report">
      <p role="status" data-testid="diagnostic-parity-outcome">
        {report.outcome}
      </p>
      <p
        className={report.outcome === "matched" ? "notice" : "notice error"}
        data-testid="diagnostic-report-evidence-class"
      >
        {diagnosticEvidenceClass(report.outcome)}
      </p>
      <p>
        Original execution evidence is unchanged. Equal returns alone never
        pass: original inputs, RNG/state, environment, intents, ordered
        statuses, fills, position trajectory, account/results and observation
        coverage must meet their named semantic checks. Record UUIDs are not
        event identity.
      </p>
      <Metadata
        values={{
          "Report SHA-256": report.report_sha256,
          "Report schema": report.schema_version,
          "Algorithm version": report.algorithm_version,
          "Tolerance version": report.tolerance_version,
          "Original event count": report.original_event_count,
          "Replay event count": report.replay_event_count,
          "Event mapping counts": report.mapping_counts,
          "Claim boundary": report.claim,
        }}
      />
      <section aria-label="Named semantic checks" className="stack mt-3">
        {report.checks.map((check) => (
          <article key={check.key} className="rule-card">
            <h3>
              {check.key} · {check.status}
            </h3>
            <p>{check.definition}</p>
            <Metadata
              values={{
                "Explicit missing or mismatch reason":
                  check.reason ?? "No reason reported",
                "Original observations": check.source_count,
                "Replay observations": check.replay_count,
                "Mismatch count": check.mismatch_count,
              }}
            />
            {(check.examples?.length ?? 0) > 0 && (
              <JsonDetails
                title={`${check.key} retained semantic differences`}
                value={check.examples}
              />
            )}
          </article>
        ))}
      </section>
      <JsonDetails
        title="Exact observation scope and coverage"
        value={report.observation_scope}
      />
      <JsonDetails
        title="Predetermined field-specific numeric tolerances"
        value={report.field_tolerances}
      />
      <ul className="notice mt-3">
        {report.limitations.map((limit, index) => (
          <li key={index}>{limit}</li>
        ))}
      </ul>
      <p className="text-xs muted">
        Verification is scoped observed re-execution parity, not a causal proof,
        local counterfactual, independent trusted ledger or no-lookahead
        certification.
      </p>
    </Panel>
  );
}
function DiagnosticParityEvents({
  owner,
  runId,
  report,
  selection,
  update,
}: {
  owner: string;
  runId: string;
  report: DiagnosticParityView;
  selection: WorkbenchSelection;
  update: VerificationUpdate;
}) {
  const offset = selection.diagnostic_event_offset ?? 0;
  const query = useQuery(
    diagnosticParityEventsOptions(owner, runId, report.report_sha256, offset),
  );
  const page =
    query.data && !query.error && parityPageMatches(report, query.data, offset)
      ? query.data
      : undefined;
  return (
    <Panel title="Original-to-diagnostic semantic event mapping">
      <p className="notice">
        Each side retains its own decision ID and decision pre-state. Pairing
        uses the exact data event, callback phase, scope and event sequence.
        Dates, symbols, equal returns and copied IDs cannot establish a verified
        mapping.
      </p>
      {query.isPending && (
        <Loading text="Reading bounded exact event mappings…" />
      )}
      {query.error && (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      )}
      {query.data && !query.error && !page && (
        <ErrorState
          error={
            new Error(
              "Event page hash, range or mapping disagrees with the immutable semantic report. It was not displayed.",
            )
          }
        />
      )}
      {page && (
        <>
          <ol className="stack" data-testid="diagnostic-event-mappings">
            {page.items.map((event, index) => {
              const original = parityReplayTarget(
                  report.source,
                  event.original_decision_id,
                ),
                replay = parityReplayTarget(
                  report.replay,
                  event.replay_decision_id,
                );
              return (
                <li
                  key={`${offset + index}:${JSON.stringify(event.pairing_key)}`}
                  className="rule-card"
                >
                  <Metadata
                    values={{
                      "Exact semantic pairing key": event.pairing_key,
                      Mapping: event.mapping,
                      "Original decision ID":
                        event.original_decision_id ?? "Unpaired",
                      "New diagnostic decision ID":
                        event.replay_decision_id ?? "Unpaired",
                      "Original pre-state SHA-256":
                        event.original_pre_state_sha256 ?? "Unavailable",
                      "Diagnostic pre-state SHA-256":
                        event.replay_pre_state_sha256 ?? "Unavailable",
                      "Both pre-state comparison": event.pre_state_status,
                      "Observation comparison": event.observation_status,
                      Reason: event.reason,
                    }}
                  />
                  <div className="flex flex-wrap gap-3 mt-3">
                    {original && (
                      <Button asChild variant="outline">
                        <Link {...original}>
                          Open original decision and pre-state
                        </Link>
                      </Button>
                    )}
                    {replay && (
                      <Button asChild variant="outline">
                        <Link {...replay}>
                          Open new diagnostic decision and pre-state
                        </Link>
                      </Button>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
          <Pagination
            offset={offset}
            limit={25}
            count={page.total}
            busy={query.isFetching}
            onPage={(value) => update({ diagnostic_event_offset: value })}
          />
        </>
      )}
    </Panel>
  );
}
