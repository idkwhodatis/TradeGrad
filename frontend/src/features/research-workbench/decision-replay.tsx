import { Suspense, lazy, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type {
  ReplayMarketPage,
  WorkbenchEvaluation,
  TraceManifestPublic,
} from "@/client/types.gen";
import { Panel, ErrorState, Loading } from "@/components/common";
import { Metadata } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { selectedDecisionOptions } from "@/lib/workbench-api";
import {
  replayCandidatesOptions,
  replayEventsOptions,
  replayExecutionOptions,
  replayInstrumentsOptions,
  replayMarketOptions,
} from "@/lib/replay-api";
import {
  candidatePageMatches,
  chooseEvent,
  decisionMatches,
  eventPageMatches,
  executionPageMatches,
  instrumentPageMatches,
  marketPageMatches,
  observationValue,
  replayAdmitted,
  selectedRecord,
} from "@/lib/replay-evidence";
import type { WorkbenchSelection } from "@/lib/workbench-selection";
import { AnchoredSource } from "./anchored-source";
import { ReplayDecisionIdentity } from "./replay-timing";
import {
  CandidateFilters,
  CandidateObservations,
  ConditionsAndFeatures,
  EvidencePage,
  EvidenceTable,
  PortfolioAndExecution,
  RecordedCollection,
  type ReplayUpdate,
} from "./replay-panels";
const ReplayChart = lazy(() => import("./replay-chart"));
const discardRecord = {
  record_kind: undefined,
  record_id: undefined,
  record_ordinal: undefined,
  rule_id: undefined,
};
const discardDetail = {
  ...discardRecord,
  detail_offset: undefined,
  execution_offset: undefined,
};

export function DecisionReplay({
  selection,
  update,
  leaf,
  trace,
  verified,
  code,
  codeSha256,
}: {
  selection: WorkbenchSelection;
  update: ReplayUpdate;
  leaf?: WorkbenchEvaluation;
  trace?: TraceManifestPublic;
  verified: boolean;
  code: string;
  codeSha256: string;
}) {
  const ready = replayAdmitted(selection, trace, verified);
  const events = useQuery({
    ...replayEventsOptions(selection),
    enabled: ready,
  });
  const instruments = useQuery({
    ...replayInstrumentsOptions(selection),
    enabled: ready,
  });
  const detail = useQuery({
    ...selectedDecisionOptions(selection),
    enabled: ready && Boolean(selection.decision_id),
  });
  const validDetail =
    ready &&
    detail.data &&
    decisionMatches(selection, detail.data) &&
    (!selection.session_date ||
      detail.data.decision?.session_date === selection.session_date)
      ? detail.data
      : undefined;
  const recordSelection = { ...selection, detail_offset: 0 };
  const recordPage = useQuery({
    ...selectedDecisionOptions(recordSelection, selection.record_ordinal),
    enabled: Boolean(
      validDetail &&
      selection.record_ordinal !== undefined &&
      (selection.record_kind === "feature" ||
        selection.record_kind === "predicate"),
    ),
  });
  const validRecordPage =
    validDetail &&
    recordPage.data &&
    decisionMatches(recordSelection, recordPage.data)
      ? recordPage.data
      : undefined;
  const candidates = useQuery({
    ...replayCandidatesOptions(selection),
    enabled: Boolean(validDetail),
  });
  const selectedCandidate = useQuery({
    ...replayCandidatesOptions(selection, true),
    enabled: Boolean(
      validDetail &&
      selection.instrument_id &&
      selection.record_kind === "candidate",
    ),
  });
  const market = useQuery({
    ...replayMarketOptions(selection),
    enabled: Boolean(validDetail && selection.instrument_id),
  });
  const execution = useQuery({
    ...replayExecutionOptions(selection),
    enabled: Boolean(validDetail),
  });
  const validEvents =
    ready && events.data && eventPageMatches(selection, events.data)
      ? events.data
      : undefined;
  const validInstruments =
    ready &&
    instruments.data &&
    instrumentPageMatches(selection, instruments.data)
      ? instruments.data
      : undefined;
  const validCandidates =
    validDetail &&
    candidates.data &&
    candidatePageMatches(selection, candidates.data)
      ? candidates.data
      : undefined;
  const validSelectedCandidate =
    validDetail &&
    selectedCandidate.data &&
    candidatePageMatches(selection, selectedCandidate.data, true)
      ? selectedCandidate.data
      : undefined;
  const validMarket =
    validDetail &&
    market.data &&
    marketPageMatches(selection, market.data, validDetail)
      ? market.data
      : undefined;
  const validExecution =
    validDetail &&
    execution.data &&
    executionPageMatches(selection, execution.data)
      ? execution.data
      : undefined;
  const recorded = selectedRecord(
    selection,
    selection.record_ordinal !== undefined &&
      ["feature", "predicate"].includes(selection.record_kind ?? "")
      ? validRecordPage
      : validDetail,
    validSelectedCandidate,
  );
  // Resolve a bare decision deep link from its actual saved header, never by
  // picking the first event on a date or substituting a similarly timed event.
  useEffect(() => {
    if (validDetail?.decision && !selection.session_date)
      update({ session_date: validDetail.decision.session_date }, true);
  }, [validDetail?.decision, selection.session_date, update]);
  useEffect(() => {
    if (
      !validDetail?.decision ||
      !leaf ||
      typeof leaf.window.start !== "string" ||
      typeof leaf.window.end !== "string" ||
      selection.market_start ||
      selection.market_end
    )
      return;
    const date = Date.parse(validDetail.decision.session_date);
    const start = new Date(date - 14 * 86400000).toISOString().slice(0, 10);
    const end = new Date(date + 15 * 86400000).toISOString().slice(0, 10);
    update(
      {
        market_start: start < leaf.window.start ? leaf.window.start : start,
        market_end: end > leaf.window.end ? leaf.window.end : end,
      },
      true,
    );
  }, [
    validDetail?.decision,
    leaf,
    selection.market_start,
    selection.market_end,
    update,
  ]);
  if (!ready)
    return (
      <Panel title="Decision event selection">
        <p className="notice">
          {!selection.evaluation_id
            ? "Choose an actual leaf Evaluation before opening decision replay."
            : !verified
              ? "Waiting for exact source and leaf verification. No replay evidence is loaded before its source association is verified."
              : "This pinned execution has no available detailed trace. Missing observations remain unknown; no explanation is reconstructed from trades."}
        </p>
        {trace?.context.missing_reason && (
          <p className="notice">{trace.context.missing_reason}</p>
        )}
        <DiagnosticUnavailable verified={verified} update={update} />
      </Panel>
    );
  const eventChoices = validEvents?.items ?? [];
  const observedStages =
    validDetail?.stages?.map((stage) => stage.stage_id) ?? [];
  const knownStages = [
    ...new Set([
      ...Object.keys(trace?.manifest?.stage_coverage ?? {}),
      ...observedStages,
      ...(selection.stage_id ? [selection.stage_id] : []),
    ]),
  ];
  return (
    <>
      <Panel title="Decision event selection">
        <p className="notice">
          Choose a date and then one specific event. Same-day decisions remain
          separate by saved decision ID, source event, callback phase and
          sequence. Market timestamps are shown separately from order and fill
          times.
        </p>
        <div className="replay-filter-grid">
          <label className="workbench-field">
            Decision date
            <input
              type="date"
              value={selection.session_date ?? ""}
              min={
                typeof leaf?.window.start === "string"
                  ? leaf.window.start
                  : undefined
              }
              max={
                typeof leaf?.window.end === "string"
                  ? leaf.window.end
                  : undefined
              }
              onChange={(event) =>
                update({
                  session_date: event.target.value || undefined,
                  decision_id: undefined,
                  stage_id: undefined,
                  instrument_id: undefined,
                  event_offset: undefined,
                  candidate_offset: undefined,
                  market_offset: undefined,
                  market_start: undefined,
                  market_end: undefined,
                  ...discardDetail,
                })
              }
            />
          </label>
          <label className="workbench-field">
            Specific decision event
            <select
              value={selection.decision_id ?? ""}
              onChange={(event) => {
                const found = eventChoices.find(
                  (item) => item.decision_id === event.target.value,
                );
                if (found)
                  update({
                    ...chooseEvent(found),
                    market_start: undefined,
                    market_end: undefined,
                  });
                else if (!event.target.value)
                  update({
                    decision_id: undefined,
                    stage_id: undefined,
                    instrument_id: undefined,
                    ...discardDetail,
                  });
              }}
            >
              <option value="">Select a specific saved event</option>
              {selection.decision_id &&
                !eventChoices.some(
                  (item) => item.decision_id === selection.decision_id,
                ) && (
                  <option value={selection.decision_id}>
                    Pinned event {selection.decision_id} (not on this index
                    page)
                  </option>
                )}
              {eventChoices.map((item) => (
                <option key={item.decision_id} value={item.decision_id}>
                  {item.session_date} · {item.decision_time} ·{" "}
                  {item.callback_phase} #{item.event_seq} · {item.scope} ·{" "}
                  {item.decision_id}
                </option>
              ))}
            </select>
          </label>
          <label className="workbench-field">
            Observed instrument
            <select
              value={selection.instrument_id ?? ""}
              onChange={(event) =>
                update({
                  instrument_id: event.target.value || undefined,
                  market_offset: undefined,
                  ...discardDetail,
                })
              }
            >
              <option value="">Whole portfolio</option>
              {selection.instrument_id &&
                !validInstruments?.items.some(
                  (item) => item.instrument_id === selection.instrument_id,
                ) && (
                  <option value={selection.instrument_id}>
                    {selection.instrument_id} (selected recorded ID; mapping not
                    on this page)
                  </option>
                )}
              {validInstruments?.items.map((item) => (
                <option key={item.instrument_id} value={item.instrument_id}>
                  {item.symbol} · {item.instrument_id} · configured session
                  member: {observationValue(item.configured_session_member)}
                </option>
              ))}
            </select>
          </label>
          <label className="workbench-field">
            Observed stage
            <select
              value={selection.stage_id ?? ""}
              onChange={(event) =>
                update({
                  stage_id: event.target.value || undefined,
                  candidate_offset: undefined,
                  ...discardDetail,
                })
              }
            >
              <option value="">All recorded stages</option>
              {knownStages.map((stage) => (
                <option key={stage}>{stage}</option>
              ))}
            </select>
          </label>
        </div>
        {events.error && <ErrorState error={events.error} />}
        {instruments.error && <ErrorState error={instruments.error} />}
        {events.isFetching && (
          <Loading text="Loading bounded saved decision events…" />
        )}
        {events.data && !validEvents && <Mismatch label="Decision index" />}
        {instruments.data && !validInstruments && (
          <Mismatch label="Instrument membership" />
        )}
        {validEvents && (
          <EvidencePage
            title="Decision events"
            {...validEvents}
            count={validEvents.items?.length ?? null}
            busy={events.isFetching}
            onPage={(event_offset) => update({ event_offset })}
          />
        )}
        {validInstruments && (
          <>
            <EvidencePage
              title="Configured instruments"
              {...validInstruments}
              count={validInstruments.items.length}
              busy={instruments.isFetching}
              onPage={(instrument_offset) => update({ instrument_offset })}
            />
            <p className="notice">
              {validInstruments.market.membership_reason} Historical membership
              status: {validInstruments.market.historical_membership_status}. A
              configured member is not automatically a genuine historical
              point-in-time member.
            </p>
          </>
        )}
        {!selection.decision_id && (
          <p className="notice">
            No event selected. A date alone is never used to choose or explain a
            decision.
          </p>
        )}
        {detail.error && <ErrorState error={detail.error} />}
        {detail.isFetching && (
          <Loading text="Loading this exact decision’s saved observations…" />
        )}
        {detail.data && !validDetail && (
          <Mismatch label="Decision details or session date" />
        )}
        {validDetail?.decision && (
          <ReplayDecisionIdentity
            decision={validDetail.decision}
            context={validDetail.context}
          />
        )}
        <DiagnosticUnavailable verified={verified} update={update} />
      </Panel>
      {selection.decision_id && (
        <div className="replay-zones">
          <Panel title="Market retrospective and saved features">
            {!selection.instrument_id ? (
              <p className="notice">
                Choose an observed instrument or click a candidate to inspect
                its fixed snapshot prices. Portfolio selection does not invent a
                composite market price.
              </p>
            ) : (
              <>
                {market.error && <ErrorState error={market.error} />}
                {market.isFetching && (
                  <Loading text="Loading immutable retrospective price page…" />
                )}
                {market.data && !validMarket && (
                  <Mismatch label="Market response" />
                )}
                {validMarket && (
                  <>
                    <MarketWindow
                      key={`${validMarket.start}:${validMarket.end}`}
                      market={validMarket}
                      update={update}
                    />
                    <Metadata
                      values={{
                        "Frozen dataset": validMarket.market.dataset_version_id,
                        "Dataset SHA-256": validMarket.market.dataset_sha256,
                        "Frozen universe":
                          validMarket.market.universe_version_id,
                        "Universe SHA-256": validMarket.market.universe_sha256,
                        "Universe state": validMarket.market.universe_state,
                        "Universe as of": validMarket.market.universe_as_of,
                        "Historical membership":
                          validMarket.market.historical_membership_status,
                        "Configured session member": observationValue(
                          validMarket.instrument?.configured_session_member,
                        ),
                        "Market timezone": validMarket.market.timezone,
                        "Price status": validMarket.status,
                      }}
                    />
                    <p className="notice">
                      {validMarket.market.membership_reason}
                    </p>
                    {validMarket.missing_reason && (
                      <p className="notice">{validMarket.missing_reason}</p>
                    )}
                    {validMarket.market.warnings.map((w) => (
                      <p className="notice" key={w}>
                        {w}
                      </p>
                    ))}
                    {validMarket.bars?.length &&
                    validMarket.information_cutoff &&
                    validMarket.decision_time ? (
                      <Suspense
                        fallback={<Loading text="Loading saved-price chart…" />}
                      >
                        <ReplayChart
                          bars={validMarket.bars}
                          features={validDetail?.features.items ?? null}
                          instrument={selection.instrument_id}
                          cutoff={validMarket.information_cutoff}
                          decisionTime={validMarket.decision_time}
                          timezone={validMarket.market.timezone}
                          context={validMarket.context}
                        />
                      </Suspense>
                    ) : null}
                    <details className="mt-3">
                      <summary>Accessible frozen price rows</summary>
                      <EvidenceTable
                        headers={[
                          "Session",
                          "Open",
                          "High",
                          "Low",
                          "Close",
                          "Volume / unit",
                          "Flags",
                        ]}
                      >
                        {validMarket.bars?.map((bar) => (
                          <tr key={`${bar.listing_id}:${bar.session_date}`}>
                            <td>{bar.session_date}</td>
                            <td>{bar.open}</td>
                            <td>{bar.high}</td>
                            <td>{bar.low}</td>
                            <td>
                              {bar.close} {bar.currency}
                            </td>
                            <td>
                              {bar.volume} {bar.volume_unit}
                            </td>
                            <td>{observationValue(bar.flags)}</td>
                          </tr>
                        ))}
                      </EvidenceTable>
                    </details>
                    <EvidencePage
                      title="Frozen prices"
                      {...validMarket}
                      count={validMarket.bars?.length ?? null}
                      busy={market.isFetching}
                      onPage={(market_offset) => update({ market_offset })}
                    />
                    <p className="notice">
                      Only this event’s saved features from the current detail
                      page can overlay the chart. Later prices are hindsight,
                      never used to explain earlier inputs. Feature validity,
                      missing points and full precision remain in the evidence
                      panel.
                    </p>
                  </>
                )}
              </>
            )}
          </Panel>
          <Panel title="Candidate observations">
            <CandidateFilters
              key={JSON.stringify([
                selection.eligibility,
                selection.reason,
                selection.rank_min,
                selection.rank_max,
                selection.score_min,
                selection.score_max,
              ])}
              selection={selection}
              update={update}
            />
            {candidates.error && <ErrorState error={candidates.error} />}
            {candidates.isFetching && (
              <Loading text="Loading filtered saved candidate page…" />
            )}
            {candidates.data && !validCandidates && (
              <Mismatch label="Candidate response" />
            )}
            {validCandidates && (
              <CandidateObservations
                page={validCandidates}
                selection={selection}
                update={update}
                busy={candidates.isFetching}
              />
            )}
          </Panel>
          <Panel title="Actual conditions and features">
            {validDetail ? (
              <ConditionsAndFeatures
                detail={validDetail}
                selection={selection}
                update={update}
                busy={detail.isFetching}
              />
            ) : (
              <p className="notice">
                Waiting for matching saved decision details. No previous event’s
                operands or features are shown.
              </p>
            )}
          </Panel>
          <Panel title="Portfolio and execution">
            {validDetail && (
              <PortfolioAndExecution
                detail={validDetail}
                selection={selection}
                update={update}
              />
            )}
            <h3 className="mt-4">Actual linked execution records</h3>
            {execution.error && <ErrorState error={execution.error} />}
            {execution.isFetching && (
              <Loading text="Loading explicitly linked execution evidence…" />
            )}
            {execution.data && !validExecution && (
              <Mismatch label="Execution response" />
            )}
            {validExecution && (
              <>
                <p>
                  Status: {validExecution.status}.{" "}
                  {validExecution.missing_reason}
                </p>
                {validExecution.warnings.map((w) => (
                  <p className="notice" key={w}>
                    {w}
                  </p>
                ))}
                {validExecution.items?.map((row, i) => (
                  <article
                    key={`${row.stage_id}:${row.logical_order_id}:${row.record_kind}:${row.record_id}:${i}`}
                    className="replay-record"
                  >
                    <Button
                      variant="outline"
                      size="sm"
                      aria-label={`Open linked order group: ${row.logical_order_id} from ${row.record_kind} ${row.record_id}`}
                      onClick={() =>
                        update({
                          instrument_id: row.instrument_id,
                          stage_id: row.stage_id,
                          record_kind: "order",
                          record_id: row.logical_order_id,
                          execution_offset: undefined,
                        })
                      }
                    >
                      {row.record_kind}: {row.record_id}
                    </Button>
                    <Metadata
                      values={{
                        "Logical order": row.logical_order_id,
                        "Actual instrument": row.instrument_id,
                        "Record status": row.status,
                        "Association basis": row.association_basis,
                        "Immutable row index": observationValue(
                          row.record_index,
                        ),
                        "Missing reason": row.missing_reason ?? "None recorded",
                      }}
                    />
                    <RecordedCollection
                      title="Persisted execution row"
                      value={row.record}
                    />
                  </article>
                ))}
                <EvidencePage
                  title="Linked execution records"
                  {...validExecution}
                  count={validExecution.items?.length ?? null}
                  busy={execution.isFetching}
                  onPage={(execution_offset) => update({ execution_offset })}
                />
                {selection.record_kind === "order" && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() =>
                      update({ ...discardRecord, execution_offset: undefined })
                    }
                  >
                    Show all linked orders for this selection
                  </Button>
                )}
              </>
            )}
          </Panel>
        </div>
      )}
      {recorded && (
        <div data-testid="replay-selected-record-source">
          <Panel title="Selected actual record">
            <p>{recorded.label}</p>
            {recorded.missing_reason && (
              <p className="notice">{recorded.missing_reason}</p>
            )}
            <RecordedCollection
              title="Exact saved observation"
              value={recorded.value}
            />
            {!recorded.source_ref && (
              <p className="notice">
                Code anchor unknown. No rule or source line was inferred from a
                symbol, nearby code or missing execution.
              </p>
            )}
          </Panel>
          {recorded.source_ref && (
            <AnchoredSource
              code={code}
              codeSha256={codeSha256}
              anchor={recorded.source_ref}
            />
          )}
        </div>
      )}
      {recordPage.error && <ErrorState error={recordPage.error} />}
      {selectedCandidate.error && (
        <ErrorState error={selectedCandidate.error} />
      )}
    </>
  );
}
function Mismatch({ label }: { label: string }) {
  return (
    <p className="notice error" role="alert">
      {label} does not match the pinned execution and selection. Stale or
      mismatched evidence was not displayed.
    </p>
  );
}
function DiagnosticUnavailable({
  verified,
  update,
}: {
  verified: boolean;
  update: ReplayUpdate;
}) {
  return (
    <div className="mt-3">
      <Button
        disabled={!verified}
        variant="outline"
        size="sm"
        onClick={() => update({ tab: "verification" })}
      >
        {verified
          ? "Inspect independent diagnostic capability"
          : "Diagnostic replay unavailable until source admission"}
      </Button>
      <p className="text-xs muted">
        Inspect frozen replay and RNG capability before creating a new
        independent diagnostic. Existing evidence is immutable; missing internal
        instrumentation cannot be reconstructed by this page.
      </p>
    </div>
  );
}
function MarketWindow({
  market,
  update,
}: {
  market: ReplayMarketPage;
  update: ReplayUpdate;
}) {
  const [error, setError] = useState<string | null>(null);
  return (
    <form
      className="replay-filter-grid"
      onSubmit={(event) => {
        event.preventDefault();
        const fields = new FormData(event.currentTarget);
        const start = String(fields.get("start"));
        const end = String(fields.get("end"));
        if (
          !start ||
          !end ||
          start >= end ||
          start < market.market.window_start ||
          end > market.market.window_end
        ) {
          setError(
            "Choose a nonempty [start, end) inside the exact leaf execution window.",
          );
          return;
        }
        setError(null);
        update({
          market_start: start,
          market_end: end,
          market_offset: undefined,
        });
      }}
    >
      <label className="workbench-field">
        Retrospective start
        <input
          name="start"
          type="date"
          required
          defaultValue={market.start}
          min={market.market.window_start}
          max={market.market.window_end}
        />
      </label>
      <label className="workbench-field">
        Retrospective end (exclusive)
        <input
          name="end"
          type="date"
          required
          defaultValue={market.end}
          min={market.market.window_start}
          max={market.market.window_end}
        />
      </label>
      <Button size="sm">Apply chart window</Button>
      {error && (
        <p role="alert" className="notice error">
          {error}
        </p>
      )}
    </form>
  );
}
