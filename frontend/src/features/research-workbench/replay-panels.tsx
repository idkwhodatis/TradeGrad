import { useState, type ReactNode } from "react";
import type {
  CandidatePage,
  DecisionDetail,
  OrderLink,
} from "@/client/types.gen";
import { Metadata, Pagination } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import {
  candidateReasons,
  eligibilityValues,
  type WorkbenchSelection,
} from "@/lib/workbench-selection";
import {
  inspectObservation,
  marketTime,
  observationValue,
} from "@/lib/replay-evidence";

export type ReplayUpdate = (
  patch: Partial<WorkbenchSelection>,
  replace?: boolean,
) => void;
export function EvidencePage({
  title,
  total,
  offset,
  limit,
  count,
  busy,
  onPage,
}: {
  title: string;
  total: number | null;
  offset: number;
  limit: number;
  count: number | null;
  busy?: boolean;
  onPage: (offset: number) => void;
}) {
  return (
    <section aria-label={`${title} pagination`} className="mt-3">
      <p className="text-xs muted">
        {title}:{" "}
        {count === null
          ? "Not collected; no empty set inferred."
          : `${count} returned on this page; ${total === null ? "total unknown" : `${total} matching saved records`}. Counts describe persisted observations, not the complete calculation universe.`}
      </p>
      {total !== null && (
        <Pagination
          offset={offset}
          limit={limit}
          count={Math.min(total, 100000 + limit)}
          busy={busy}
          onPage={(next) => onPage(Math.min(100000, next))}
        />
      )}
      {total !== null && total > 100000 && (
        <p className="notice">
          Browser offset is bounded at 100,000. Narrow the date, stage or
          filters to inspect later evidence; the remaining records were not
          discarded.
        </p>
      )}
    </section>
  );
}
export function EvidenceTable({
  headers,
  children,
  testId,
}: {
  headers: string[];
  children: ReactNode;
  testId?: string;
}) {
  return (
    <div className="replay-table-scroll">
      <table className="replay-table" data-testid={testId}>
        <thead>
          <tr>
            {headers.map((h) => (
              <th key={h}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
export function CandidateFilters({
  selection,
  update,
}: {
  selection: WorkbenchSelection;
  update: ReplayUpdate;
}) {
  const [error, setError] = useState<string | null>(null);
  return (
    <form
      className="replay-filter-grid"
      aria-label="Candidate observation filters"
      onSubmit={(event) => {
        event.preventDefault();
        const values = new FormData(event.currentTarget);
        const number = (key: string) => {
          const v = String(values.get(key) ?? "").trim();
          return v ? Number(v) : undefined;
        };
        const rank_min = number("rank_min"),
          rank_max = number("rank_max"),
          score_min = number("score_min"),
          score_max = number("score_max");
        if (
          [rank_min, rank_max, score_min, score_max].some(
            (n) => n !== undefined && !Number.isFinite(n),
          ) ||
          [rank_min, rank_max].some(
            (n) => n !== undefined && (!Number.isSafeInteger(n) || n < 1),
          ) ||
          (rank_min !== undefined &&
            rank_max !== undefined &&
            rank_min > rank_max) ||
          (score_min !== undefined &&
            score_max !== undefined &&
            score_min > score_max)
        ) {
          setError(
            "Use finite score bounds and positive integer rank bounds, with minimum no greater than maximum.",
          );
          return;
        }
        setError(null);
        update({
          eligibility: (String(values.get("eligibility")) ||
            undefined) as WorkbenchSelection["eligibility"],
          reason: (String(values.get("reason")) ||
            undefined) as WorkbenchSelection["reason"],
          rank_min,
          rank_max,
          score_min,
          score_max,
          candidate_offset: undefined,
        });
      }}
    >
      <label className="workbench-field">
        Eligibility filter
        <select name="eligibility" defaultValue={selection.eligibility ?? ""}>
          <option value="">All recorded eligibility states</option>
          {eligibilityValues.map((v) => (
            <option key={v}>{v}</option>
          ))}
        </select>
      </label>
      <label className="workbench-field">
        Reason filter
        <select name="reason" defaultValue={selection.reason ?? ""}>
          <option value="">All recorded reasons, including non-trades</option>
          {candidateReasons.map((v) => (
            <option key={v}>{v}</option>
          ))}
        </select>
      </label>
      {(
        [
          ["rank_min", "Minimum rank"],
          ["rank_max", "Maximum rank"],
          ["score_min", "Minimum score"],
          ["score_max", "Maximum score"],
        ] as const
      ).map(([key, label]) => (
        <label className="workbench-field" key={key}>
          {label}
          <input
            name={key}
            type="number"
            step={key.startsWith("rank") ? "1" : "any"}
            min={key.startsWith("rank") ? 1 : undefined}
            defaultValue={selection[key] ?? ""}
          />
        </label>
      ))}
      <div className="actions">
        <Button type="submit" size="sm">
          Apply candidate filters
        </Button>
        <Button
          variant="outline"
          type="button"
          size="sm"
          onClick={() =>
            update({
              eligibility: undefined,
              reason: undefined,
              rank_min: undefined,
              rank_max: undefined,
              score_min: undefined,
              score_max: undefined,
              candidate_offset: undefined,
            })
          }
        >
          Reset candidate filters
        </Button>
      </div>
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
export function CandidateObservations({
  page,
  selection,
  update,
  busy,
}: {
  page: CandidatePage;
  selection: WorkbenchSelection;
  update: ReplayUpdate;
  busy?: boolean;
}) {
  return (
    <>
      <p className="notice">
        These are actual saved candidate reasons, including non-trades.
        Selecting a symbol inspects the same portfolio decision, not a new
        per-stock decision. A missing fill never proves there was no order
        intent.
      </p>
      <EvidenceTable
        testId="replay-candidates-table"
        headers={[
          "Instrument / recorded stage",
          "Eligibility and reason",
          "Score / validity",
          "Rank / ordering",
          "Selection / prior holding",
          "Universe evidence",
          "Source",
        ]}
      >
        {page.items?.map(({ stage_id, observation: row, ordinal }, index) => (
          <tr
            key={`${stage_id}:${row.instrument_id}:${index}`}
            data-testid={`replay-candidate-${row.instrument_id}`}
            data-selected={
              selection.instrument_id === row.instrument_id &&
              selection.stage_id === stage_id
            }
          >
            <td>
              <Button
                size="sm"
                variant="outline"
                aria-label={`Inspect candidate: ${row.instrument_id}`}
                onClick={() =>
                  update({
                    instrument_id: row.instrument_id,
                    stage_id,
                    record_kind: "candidate",
                    record_id: row.instrument_id,
                    record_ordinal: ordinal,
                    rule_id: undefined,
                    detail_offset: undefined,
                    execution_offset: undefined,
                    market_offset: undefined,
                    candidate_offset: undefined,
                  })
                }
              >
                {row.instrument_id}
              </Button>
              <div>{stage_id}</div>
            </td>
            <td>
              {row.eligibility}
              <br />
              {row.reason}
              <br />
              {row.elimination_stage
                ? `Eliminated at ${row.elimination_stage}`
                : "Elimination stage not recorded"}
              <p>{row.reason_detail ?? "No additional reason recorded"}</p>
            </td>
            <td>
              {observationValue(row.score)}
              <br />
              {row.score_validity}
            </td>
            <td>
              {observationValue(row.rank)}
              <br />
              {row.ranking_direction ?? "Direction not recorded"}
              <br />
              {row.tie_break ?? "Tie rule not recorded"}
            </td>
            <td>
              Selected: {observationValue(row.selected)}
              <br />
              Prior holding: {observationValue(row.prior_holding)}
            </td>
            <td>
              Recorded membership: {observationValue(row.universe_member)}
              <br />
              <small>
                Observation in configured universe; not proof of historical
                point-in-time membership.
              </small>
            </td>
            <td>
              {row.source}
              <br />
              {row.source_ref
                ? `${row.source_ref.file}:${row.source_ref.line_start}`
                : "Code anchor unknown"}
            </td>
          </tr>
        ))}
      </EvidenceTable>
      {page.items?.length === 0 && (
        <p className="notice">
          No saved candidates match these filters. This is not evidence of an
          empty calculation universe.
        </p>
      )}
      <EvidencePage
        title="Candidates"
        {...page}
        count={page.items?.length ?? null}
        busy={busy}
        onPage={(candidate_offset) => update({ candidate_offset })}
      />
    </>
  );
}
export function ConditionsAndFeatures({
  detail,
  selection,
  update,
  busy,
}: {
  detail: DecisionDetail;
  selection: WorkbenchSelection;
  update: ReplayUpdate;
  busy?: boolean;
}) {
  const timezone = detail.decision?.market_timezone ?? "UTC";
  return (
    <>
      <p className="notice">
        Actual operands and saved values only. false, not_evaluated, unknown and
        not_sampled remain different. No expression or indicator is re-evaluated
        while browsing.
      </p>
      <h3>Recorded predicates</h3>
      <EvidenceTable
        testId="replay-predicates-table"
        headers={[
          "Condition / stage / instrument",
          "Operands",
          "Operator",
          "Threshold",
          "State / reason",
          "Source",
        ]}
      >
        {detail.predicates.items?.map(
          ({ stage_id, observation: row, ordinal }, index) => (
            <tr
              key={`${stage_id}:${row.instrument_id}:${row.condition_id}:${index}`}
              data-testid={`replay-predicate-${row.condition_id}`}
            >
              <td>
                <Button
                  variant="outline"
                  size="sm"
                  aria-label={`Inspect condition: ${row.condition_id}`}
                  onClick={() =>
                    update(
                      inspectObservation(selection, "predicate", {
                        stage_id,
                        ordinal,
                        observation: row,
                      }),
                    )
                  }
                >
                  {row.condition_id}
                </Button>
                <div>
                  {stage_id} · {row.instrument_id ?? "Portfolio"}
                </div>
              </td>
              <td>{observationValue(row.operands)}</td>
              <td>{row.operator}</td>
              <td>{observationValue(row.threshold)}</td>
              <td>
                {row.state}
                <br />
                {row.missing_reason ?? "No missing reason recorded"}
              </td>
              <td>
                {row.source}
                <br />
                {row.source_ref
                  ? `${row.source_ref.file}:${row.source_ref.line_start}`
                  : "Code anchor unknown"}
              </td>
            </tr>
          ),
        )}
      </EvidenceTable>
      <EvidencePage
        title="Predicates"
        {...detail.predicates}
        count={detail.predicates.items?.length ?? null}
        busy={busy}
        onPage={(detail_offset) =>
          update({
            detail_offset,
            record_kind: undefined,
            record_id: undefined,
          })
        }
      />
      <h3 className="mt-4">Recorded features</h3>
      <EvidenceTable
        testId="replay-features-table"
        headers={[
          "Feature / stage / instrument",
          "Actual value / unit",
          "Validity / missing reason",
          "Calculation window",
          "Information cutoff",
          "Source",
        ]}
      >
        {detail.features.items?.map(
          ({ stage_id, observation: row, ordinal }, index) => (
            <tr
              key={`${stage_id}:${row.instrument_id}:${row.feature_id}:${index}`}
              data-testid={`replay-feature-${row.feature_id}`}
            >
              <td>
                <Button
                  variant="outline"
                  size="sm"
                  aria-label={`Inspect feature: ${row.feature_id}`}
                  onClick={() =>
                    update(
                      inspectObservation(selection, "feature", {
                        stage_id,
                        ordinal,
                        observation: row,
                      }),
                    )
                  }
                >
                  {row.feature_id}
                </Button>
                <div>
                  {stage_id} · {row.instrument_id ?? "Portfolio"}
                </div>
              </td>
              <td>
                {observationValue(row.value)} {row.unit}
              </td>
              <td>
                {row.validity}
                <br />
                {row.missing_reason ?? "No missing reason recorded"}
              </td>
              <td>{row.calculation_window}</td>
              <td>{marketTime(row.information_cutoff, timezone)}</td>
              <td>
                {row.source}
                <br />
                {row.source_ref
                  ? `${row.source_ref.file}:${row.source_ref.line_start}`
                  : "Code anchor unknown"}
              </td>
            </tr>
          ),
        )}
      </EvidenceTable>
      <EvidencePage
        title="Features"
        {...detail.features}
        count={detail.features.items?.length ?? null}
        busy={busy}
        onPage={(detail_offset) =>
          update({
            detail_offset,
            record_kind: undefined,
            record_id: undefined,
          })
        }
      />
      <p className="text-xs muted">
        Features and predicates use the same explicit detail offset. Each
        collection reports its own total; changing pages never fills absent rows
        with false or zero.
      </p>
    </>
  );
}
export function RecordedCollection({
  title,
  value,
}: {
  title: string;
  value: unknown;
}) {
  return (
    <section className="replay-record">
      <h4>{title}</h4>
      <pre className="preview" tabIndex={0}>
        {value === null || value === undefined
          ? "Not recorded"
          : Array.isArray(value) && !value.length
            ? "Observed empty []"
            : JSON.stringify(value, null, 2)}
      </pre>
    </section>
  );
}
export function OrderObservation({
  order,
  stage,
  timezone,
  update,
}: {
  order: OrderLink;
  stage: string;
  timezone: string;
  update: ReplayUpdate;
}) {
  return (
    <article
      className="replay-record"
      data-testid={`replay-execution-${order.logical_order_id}`}
    >
      <Button
        variant="outline"
        size="sm"
        aria-label={`Inspect order: ${order.logical_order_id}`}
        onClick={() =>
          update({
            stage_id: stage,
            instrument_id: order.instrument_id,
            record_kind: "order",
            record_id: order.logical_order_id,
            rule_id: undefined,
            detail_offset: undefined,
            execution_offset: undefined,
            market_offset: undefined,
          })
        }
      >
        {order.logical_order_id}
      </Button>
      <Metadata
        values={{
          Instrument: order.instrument_id,
          "Recorded stage": stage,
          "Actual order state": order.status,
          "Submitted at": marketTime(order.submitted_at, timezone),
          "Child order IDs": observationValue(order.child_order_ids),
          "Explicit fill IDs": observationValue(order.fill_ids),
          "Fill times": order.fill_times.length
            ? order.fill_times.map((time) => marketTime(time, timezone))
            : "No fill times saved",
          "Rejection reason": order.rejection_reason ?? "Not recorded",
          Source: order.source,
        }}
      />
    </article>
  );
}
export function PortfolioAndExecution({
  detail,
  selection,
  update,
}: {
  detail: DecisionDetail;
  selection: WorkbenchSelection;
  update: ReplayUpdate;
}) {
  const decision = detail.decision;
  if (!decision)
    return <p className="notice">Decision and pre-state were not collected.</p>;
  const state = decision.pre_state;
  return (
    <>
      <div data-testid="replay-pre-state">
        <h3>Decision-preceding account and strategy state</h3>
        {state ? (
          <>
            <Metadata
              values={{
                "Pre-state coverage": state.coverage,
                "Available cash": observationValue(state.cash),
                Equity: observationValue(state.equity),
                Source: state.source,
                "Missing reason": state.missing_reason ?? "None recorded",
              }}
            />
            <div className="replay-record-grid">
              <RecordedCollection
                title="Pre-decision positions"
                value={state.positions}
              />
              <RecordedCollection
                title="Pre-decision open orders"
                value={state.open_orders}
              />
              <RecordedCollection
                title="Pre-decision strategy state"
                value={state.strategy_state}
              />
            </div>
          </>
        ) : (
          <p className="notice">
            Pre-decision state: Not recorded.{" "}
            {decision.pre_state_missing_reason ?? "No reason available"}.
            Missing state is not zero cash or zero positions.
          </p>
        )}
      </div>
      {detail.stages === null ? (
        <p className="notice">Stages not collected.</p>
      ) : detail.stages.length === 0 ? (
        <p className="notice">
          No saved stage matches this selection. Branch outcome unknown.
        </p>
      ) : (
        detail.stages.map((stage) => (
          <section key={stage.stage_id} className="mt-4">
            <h3>{stage.stage_id}</h3>
            <p>
              Coverage: {stage.coverage} ·{" "}
              {stage.missing_reason ?? "No missing reason recorded"}
            </p>
            <p className="text-xs muted">
              Saved counts: {stage.features_saved} features ·{" "}
              {stage.predicates_saved} predicates · {stage.candidates_saved}{" "}
              candidates.
            </p>
            <div className="replay-record-grid">
              <RecordedCollection
                title="Raw targets"
                value={stage.raw_targets}
              />
              <RecordedCollection
                title="Constrained plan"
                value={stage.constrained_plan}
              />
            </div>
            <h4>
              Explicit decision → logical order → child order → fill
              associations
            </h4>
            {stage.order_links.length === 0 ? (
              <p className="notice">
                No order links saved for this stage. This does not prove no
                order intent; inspect its coverage and targets.
              </p>
            ) : (
              stage.order_links
                .filter(
                  (order) =>
                    !selection.instrument_id ||
                    order.instrument_id === selection.instrument_id,
                )
                .map((order, index) => (
                  <OrderObservation
                    key={`${order.logical_order_id}:${index}`}
                    order={order}
                    stage={stage.stage_id}
                    timezone={decision.market_timezone}
                    update={update}
                  />
                ))
            )}
            {selection.instrument_id &&
              stage.order_links.length > 0 &&
              !stage.order_links.some(
                (order) => order.instrument_id === selection.instrument_id,
              ) && (
                <p className="notice">
                  No explicit links for this instrument in the saved stage.
                  Other portfolio orders remain available by choosing Whole
                  portfolio.
                </p>
              )}
          </section>
        ))
      )}
    </>
  );
}
