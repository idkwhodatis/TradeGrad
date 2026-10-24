import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type {
  ComparisonDetail,
  ComparisonPublic,
  ComparisonResult,
  WorkbenchEvaluation,
  WorkbenchSpec,
} from "@/client/types.gen";
import { ErrorState, Loading, Panel } from "@/components/common";
import { Metadata, Pagination } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { queryClient } from "@/lib/api";
import { useRunEvents } from "@/lib/use-run-events";
import {
  changeComparison,
  comparisonPageMatches,
  comparisonResultOptions,
  comparisonSectionOptions,
  comparisonStatusOptions,
  createComparison,
} from "@/lib/comparison-api";
import {
  comparisonCompletionCurrent,
  clearComparisonDraft,
  clearUnreadableComparisonDraft,
  comparisonActionKey,
  loadComparisonAction,
  storeComparisonAction,
  clearComparisonAction,
  comparisonActionResolved,
  loadComparisonDraft,
  prepareComparisonDraft,
  storeComparisonDraft,
  type ComparisonDraft,
  type PendingComparisonAction,
} from "@/lib/comparison-draft";
import {
  comparisonFingerprint,
  comparisonRequest,
  comparisonResultProblem,
  comparisonStatusProblem,
  comparisonTerminal,
} from "@/lib/comparison-evidence";
import {
  clearComparisonResult,
  clearComparisonSide,
  comparisonSections,
  type ComparisonSection,
  type WorkbenchSelection,
} from "@/lib/workbench-selection";
import { ComparisonSourcePicker } from "./comparison-source";
import {
  ComparisonAvailability,
  ComparisonConfiguration,
  ComparisonMechanisms,
  ComparisonOutcome,
  ComparisonPrestates,
  ComparisonRecords,
} from "./comparison-panels";

export function MechanismComparison({
  owner,
  selection,
  update,
  currentSpec,
  currentLeaf,
  currentVerified,
}: {
  owner: string;
  selection: WorkbenchSelection;
  update: (patch: Partial<WorkbenchSelection>, replace?: boolean) => void;
  currentSpec: WorkbenchSpec;
  currentLeaf?: WorkbenchEvaluation;
  currentVerified: boolean;
}) {
  const [initial] = useState(() => {
    try {
      return { draft: loadComparisonDraft(owner), error: null as unknown };
    } catch (error) {
      return { draft: null, error };
    }
  });
  const [draft, setDraft] = useState<ComparisonDraft | null>(initial.draft);
  const [error, setError] = useState<unknown>(initial.error);
  const [unreadable, setUnreadable] = useState(Boolean(initial.error));
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const inFlight = useRef(false);
  const current = useRef({ active: true, selection, key: draft?.key ?? null });
  current.current.selection = selection;
  current.current.key = draft?.key ?? null;
  useEffect(() => {
    current.current.active = true;
    return () => {
      current.current.active = false;
    };
  }, []);
  let requestProblem: string | null = null;
  try {
    comparisonRequest(selection);
  } catch (failure) {
    requestProblem = (failure as Error).message;
  }
  let pendingMatches = false;
  try {
    pendingMatches = Boolean(
      draft &&
      comparisonFingerprint(draft.body) ===
        comparisonFingerprint(comparisonRequest(selection)),
    );
  } catch {
    /* An incomplete new selection never matches a retained write. */
  }
  async function submit() {
    if (
      inFlight.current ||
      unreadable ||
      requestProblem ||
      (draft && !pendingMatches)
    )
      return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    let pending: ComparisonDraft | null = null;
    try {
      pending = draft || prepareComparisonDraft(selection);
      storeComparisonDraft(owner, pending); // Never send a write before retaining its exact idempotent recovery.
      setDraft(pending);
      current.current.key = pending.key;
      const response = await createComparison(pending.body, pending.key);
      if (
        comparisonFingerprint(response.request) !==
        comparisonFingerprint(pending.body)
      )
        throw new Error(
          "The accepted comparison does not match the exact saved request. It was not opened; retry the same request to resolve the outcome.",
        );
      queryClient.setQueryData(["mechanism-comparison", response.id], response);
      if (comparisonCompletionCurrent(pending, current.current)) {
        update({ ...clearComparisonResult(), comparison_id: response.id });
        setNotice(
          `Immutable comparison ${response.id} accepted. Existing frozen input evidence is read only; no strategy was re-evaluated.`,
        );
        clearComparisonDraft(owner, pending.key);
        setDraft(null);
        current.current.key = null;
      } else if (current.current.active) {
        setNotice(
          `Comparison ${response.id} was accepted for the earlier selection. Restore its retained input selection and retry the same request to open it; your newer selection was not replaced.`,
        );
      }
    } catch (failure) {
      if (current.current.active) setError(failure);
      // Network, 401, timeout and server errors all retain the exact body/key.
      // An error never proves a create did not commit.
    } finally {
      inFlight.current = false;
      if (current.current.active) setBusy(false);
    }
  }
  const status = useQuery(comparisonStatusOptions(selection.comparison_id));
  const statusProblem = status.data
    ? comparisonStatusProblem(selection, status.data)
    : null;
  const hash =
    selection.comparison_manifest_sha256 ||
    status.data?.manifest_sha256 ||
    undefined;
  const result = useQuery({
    ...comparisonResultOptions(selection.comparison_id, hash),
    enabled: Boolean(
      selection.comparison_id &&
      status.data?.status === "succeeded" &&
      status.data.artifact_ready &&
      hash &&
      !statusProblem &&
      !status.error,
    ),
  });
  const resultProblem =
    result.data && status.data
      ? comparisonResultProblem(selection, status.data, result.data)
      : null;
  const trustedResult =
    result.data &&
    !result.error &&
    !resultProblem &&
    !statusProblem &&
    !status.error
      ? result.data
      : undefined;
  useEffect(() => {
    if (trustedResult && !selection.comparison_manifest_sha256)
      update(
        { comparison_manifest_sha256: trustedResult.manifest_sha256 },
        true,
      );
  }, [trustedResult, selection.comparison_manifest_sha256, update]);
  return (
    <div className="stack">
      <Panel title="Compare two immutable evaluations">
        <p className="notice">
          Choose both sources and physical leaves explicitly. This read-only
          comparison reuses frozen results; it does not re-evaluate Python,
          download data, call a model or create a controlled experiment.
        </p>
        <div className="comparison-sides" data-testid="comparison-selection">
          {(["left", "right"] as const).map((side) => (
            <ComparisonSourcePicker
              key={side}
              side={side}
              selection={selection}
              update={update}
              currentSpec={currentSpec}
              currentLeaf={currentLeaf}
              currentVerified={currentVerified}
              disabled={busy}
            />
          ))}
        </div>
        {draft && (
          <p className="notice mt-3">
            A comparison submission is pending or its outcome is uncertain. Its
            exact ordered inputs and idempotency key are retained. Retry that
            request, not a replacement job.
          </p>
        )}
        {draft && !pendingMatches && (
          <Button
            variant="outline"
            onClick={() =>
              update({
                ...clearComparisonResult(),
                ...clearComparisonSide("left"),
                ...clearComparisonSide("right"),
                ...draft.selection,
              })
            }
          >
            Restore pending comparison selection
          </Button>
        )}
        {unreadable && (
          <Button
            variant="outline"
            onClick={() => {
              try {
                clearUnreadableComparisonDraft(owner);
                setUnreadable(false);
                setError(null);
              } catch (failure) {
                setError(failure);
              }
            }}
          >
            Clear unreadable local comparison request
          </Button>
        )}
        <Button
          className="mt-3"
          disabled={
            busy ||
            unreadable ||
            Boolean(requestProblem) ||
            Boolean(draft && !pendingMatches)
          }
          onClick={() => void submit()}
        >
          {busy
            ? "Creating immutable comparison…"
            : draft
              ? "Retry same comparison submission"
              : "Create immutable comparison"}
        </Button>
        {requestProblem && <p className="notice mt-3">{requestProblem}</p>}
        {notice && (
          <p className="notice mt-3" role="status">
            {notice}
          </p>
        )}
        {error != null && <ErrorState error={error} />}
      </Panel>
      {selection.comparison_id && (
        <>
          {status.isPending && (
            <Loading text="Reading durable comparison status…" />
          )}
          {status.error && (
            <ErrorState
              error={status.error}
              retry={() => void status.refetch()}
            />
          )}
          {statusProblem && <ErrorState error={new Error(statusProblem)} />}
          {status.data && !statusProblem && !status.error && (
            <ComparisonJob
              key={status.data.id}
              owner={owner}
              status={status.data}
            />
          )}
          {result.isFetching &&
            !trustedResult &&
            status.data?.status === "succeeded" &&
            !statusProblem && (
              <Loading text="Admitting both immutable results and comparison manifest…" />
            )}
          {result.error && (
            <ErrorState
              error={result.error}
              retry={() => void result.refetch()}
            />
          )}
          {resultProblem && <ErrorState error={new Error(resultProblem)} />}
          {trustedResult && (
            <>
              <ComparisonConfiguration manifest={trustedResult.manifest} />
              <ComparisonMechanisms
                result={trustedResult}
                selection={selection}
                update={update}
              />
              <ComparisonOutcome manifest={trustedResult.manifest} />
              <ComparisonBehavior
                key={`${trustedResult.comparison_id}:${trustedResult.manifest_sha256}`}
                result={trustedResult}
                selection={selection}
                update={update}
              />
            </>
          )}
        </>
      )}
    </div>
  );
}
function ComparisonJob({
  owner,
  status,
}: {
  owner: string;
  status: ComparisonDetail | ComparisonPublic;
}) {
  const [initialAction] = useState(() => {
    try {
      return {
        pending: loadComparisonAction(owner, status.id),
        error: null as unknown,
      };
    } catch (error) {
      return { pending: null, error };
    }
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(initialAction.error);
  const [pending, setPending] = useState<PendingComparisonAction | null>(
    initialAction.pending,
  );
  const actionBlocked =
    Boolean(initialAction.error) ||
    Boolean(pending && pending.input_sha256 !== status.input_sha256);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const terminal = comparisonTerminal.has(status.status);
  useEffect(() => {
    if (pending && comparisonActionResolved(pending, status)) {
      try {
        clearComparisonAction(owner, status.id, pending.key);
        setPending(null);
        setError(null);
      } catch (failure) {
        setError(failure);
      }
    }
  }, [
    owner,
    status.id,
    status.status,
    status.generation,
    status.input_sha256,
    pending,
  ]);
  async function act(action: "cancel" | "retry") {
    if (
      inFlight.current ||
      actionBlocked ||
      (pending && pending.action !== action)
    )
      return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const request = pending || {
        action,
        key: comparisonActionKey(owner, status.id, action, status.generation),
        generation: status.generation,
        input_sha256: status.input_sha256,
      };
      storeComparisonAction(owner, status.id, request);
      setPending(request);
      const response = await changeComparison(
        status.id,
        request.action,
        request.key,
      );
      if (
        response.id !== status.id ||
        response.input_sha256 !== status.input_sha256
      )
        throw new Error(
          "Action response belongs to a different immutable comparison; refresh status to resolve the outcome.",
        );
      queryClient.setQueryData(["mechanism-comparison", status.id], response);
      await queryClient.invalidateQueries({
        queryKey: ["mechanism-comparison", status.id],
      });
      clearComparisonAction(owner, status.id, request.key);
      if (mounted.current) setPending(null);
    } catch (failure) {
      if (mounted.current) setError(failure);
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <Panel title="Durable comparison job">
      <p data-testid="comparison-status" role="status">
        {status.status}
      </p>
      <Metadata
        values={{
          "Comparison ID": status.id,
          "Comparison run": status.run_id,
          Generation: status.generation,
          "Ordered request SHA-256": status.input_sha256,
          "Engine input SHA-256": status.engine_input_sha256,
          "Active attempt": status.active_attempt_id,
          "Comparison manifest SHA-256": status.manifest_sha256,
          "Artifact ready": status.artifact_ready,
        }}
      />
      {status.error_code && (
        <p className="notice error">
          Comparison failed or interrupted: {status.error_code}. No failed run
          is represented as a zero result.
        </p>
      )}
      {pending && (
        <p className="notice">
          The {pending.action} outcome is pending or uncertain. Retry the same
          retained action; durable status is authoritative.
        </p>
      )}
      <div className="flex gap-3 mt-3">
        {!terminal && (
          <Button
            variant="outline"
            disabled={
              busy ||
              actionBlocked ||
              Boolean(pending && pending.action !== "cancel")
            }
            onClick={() => void act("cancel")}
          >
            Cancel comparison
          </Button>
        )}
        {["failed", "cancelled", "interrupted"].includes(status.status) && (
          <Button
            variant="outline"
            disabled={
              busy ||
              actionBlocked ||
              Boolean(pending && pending.action !== "retry")
            }
            onClick={() => void act("retry")}
          >
            Retry comparison job
          </Button>
        )}
        {pending && (
          <Button
            variant="outline"
            disabled={busy || actionBlocked}
            onClick={() => void act(pending.action)}
          >
            Retry same {pending.action} action
          </Button>
        )}
        <Button
          variant="outline"
          disabled={busy}
          onClick={() =>
            void queryClient.invalidateQueries({
              queryKey: ["mechanism-comparison", status.id],
            })
          }
        >
          Refresh comparison status
        </Button>
      </div>
      {error != null && <ErrorState error={error} />}
      {"attempts" in status && (
        <details className="mt-3">
          <summary>Persisted attempts</summary>
          {status.attempts.map((attempt) => (
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
      )}
      <ComparisonEvents runId={status.run_id} terminal={terminal} />
    </Panel>
  );
}
function ComparisonEvents({
  runId,
  terminal,
}: {
  runId: string;
  terminal: boolean;
}) {
  const events = useRunEvents(runId, terminal);
  return (
    <details className="mt-3">
      <summary>Comparison progress notifications</summary>
      <p className="text-xs muted">
        Authenticated existing run events contain only the progress-safe subset.
        Database status and separately admitted artifacts are authoritative, not
        event payloads.
      </p>
      {events.error && (
        <p className="notice">
          Progress notifications are unavailable. Durable status polling
          continues.
        </p>
      )}
      {events.data?.map((event) => (
        <p key={event.seq} className="text-xs" data-event-seq={event.seq}>
          {event.seq} · {event.type} · {event.occurred_at}
        </p>
      ))}
    </details>
  );
}
function ComparisonBehavior({
  result,
  selection,
  update,
}: {
  result: ComparisonResult;
  selection: WorkbenchSelection;
  update: (patch: Partial<WorkbenchSelection>) => void;
}) {
  const section = selection.comparison_section || "events";
  const eventOffset = selection.comparison_event_offset || 0;
  const sectionOffset =
    section === "events" ? eventOffset : selection.comparison_offset || 0;
  const pair = selection.event_pair_id;
  const instrument = selection.comparison_instrument_id;
  const [instrumentInput, setInstrumentInput] = useState(instrument || "");
  useEffect(() => {
    setInstrumentInput(instrument || "");
  }, [instrument]);
  const events = useQuery(
    comparisonSectionOptions(
      result.comparison_id,
      result.manifest_sha256,
      "events",
      { offset: eventOffset },
    ),
  );
  const event = useQuery({
    ...comparisonSectionOptions(
      result.comparison_id,
      result.manifest_sha256,
      "events",
      { event_pair_id: pair },
    ),
    enabled: Boolean(pair),
  });
  const records = useQuery({
    ...comparisonSectionOptions(
      result.comparison_id,
      result.manifest_sha256,
      section,
      { event_pair_id: pair, instrument_id: instrument, offset: sectionOffset },
    ),
    enabled: section !== "events",
  });
  const validEvents =
    events.data &&
    !events.error &&
    comparisonPageMatches(events.data, result, "events", eventOffset)
      ? events.data
      : undefined;
  const validPairPage =
    event.data &&
    !event.error &&
    comparisonPageMatches(event.data, result, "events", 0, pair)
      ? event.data
      : undefined;
  const selectedEvent =
    pair && validPairPage?.total === 1 && validPairPage.items.length === 1
      ? validPairPage.items[0]
      : undefined;
  const page =
    section === "events"
      ? validEvents
      : records.data &&
          !records.error &&
          comparisonPageMatches(
            records.data,
            result,
            section,
            sectionOffset,
            pair,
            instrument,
          )
        ? records.data
        : undefined;
  const onPair = useCallback(
    (id: string) => update({ event_pair_id: id, comparison_offset: 0 }),
    [update],
  );
  const pairChoices = validEvents?.items || [];
  const allChoices =
    selectedEvent &&
    !pairChoices.some(
      (item) => item.event_pair_id === selectedEvent.event_pair_id,
    )
      ? [selectedEvent, ...pairChoices]
      : pairChoices;
  return (
    <Panel title="4. Exact event pairs and bounded behavior evidence">
      <p className="notice">
        Pairs use the saved common source-data event, callback phase, decision
        scope and sequence. Unmatched events remain added/deleted or ambiguous;
        date and symbol alone never establish identity.
      </p>
      <label className="workbench-field">
        Explicit event pair
        <select
          value={pair || ""}
          onChange={(event) =>
            update({
              event_pair_id: event.target.value || undefined,
              comparison_offset: 0,
            })
          }
        >
          <option value="">Choose a specific event pair</option>
          {allChoices
            .filter((item) => item.event_pair_id)
            .map((item) => (
              <option value={item.event_pair_id!} key={item.row_id}>
                {item.session_date || "Undated"} · {item.status} ·{" "}
                {item.event_pair_id}
              </option>
            ))}
        </select>
      </label>
      {validEvents && (
        <Pagination
          offset={eventOffset}
          limit={25}
          count={validEvents.total}
          busy={events.isFetching}
          onPage={(offset) => update({ comparison_event_offset: offset })}
        />
      )}
      {events.error && <ErrorState error={events.error} />}
      {events.data && !validEvents && (
        <ErrorState
          error={
            new Error(
              "Event-page identity differs from the immutable comparison. No rows are displayed.",
            )
          }
        />
      )}
      {pair && event.isPending && (
        <Loading text="Loading both exact event pre-states…" />
      )}
      {event.error && <ErrorState error={event.error} />}
      {pair && event.data && !selectedEvent && (
        <ErrorState
          error={
            new Error(
              "The exact selected event pair is absent or ambiguous. No first event or date-matched replacement was used.",
            )
          }
        />
      )}
      <ComparisonPrestates result={result} event={selectedEvent} />
      <label className="workbench-field">
        Behavior section
        <select
          value={section}
          onChange={(event) =>
            update({
              comparison_section: event.target.value as ComparisonSection,
              comparison_offset: 0,
            })
          }
        >
          {comparisonSections.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
      {section !== "events" && (
        <form
          className="flex items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            update({
              comparison_instrument_id: instrumentInput.trim() || undefined,
              comparison_offset: 0,
            });
          }}
        >
          <label className="workbench-field grow">
            Comparison instrument filter
            <input
              value={instrumentInput}
              maxLength={500}
              onChange={(event) => setInstrumentInput(event.target.value)}
            />
          </label>
          <Button type="submit" variant="outline">
            Apply comparison instrument filter
          </Button>
        </form>
      )}
      <ComparisonAvailability value={result.manifest.sections[section]} />
      {section !== "events" && records.error && (
        <ErrorState
          error={records.error}
          retry={() => void records.refetch()}
        />
      )}
      {section !== "events" && records.data && !page && (
        <ErrorState
          error={
            new Error(
              "Behavior-page identity or selected event range differs. No cached unrelated records were shown.",
            )
          }
        />
      )}
      {(section === "events" ? events.isPending : records.isPending) && (
        <Loading text="Reading bounded behavior evidence…" />
      )}
      {page && (
        <>
          <ComparisonRecords
            rows={page.items}
            result={result}
            selectedEvent={selectedEvent}
            selectedPair={pair}
            onPair={onPair}
          />
          {section !== "events" && (
            <Pagination
              offset={sectionOffset}
              limit={25}
              count={page.total}
              busy={records.isFetching}
              onPage={(offset) => update({ comparison_offset: offset })}
            />
          )}
        </>
      )}
    </Panel>
  );
}
