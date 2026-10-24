import { lazy, Suspense } from "react";
import { Link } from "@tanstack/react-router";
import type {
  ComparisonAssociation,
  ComparisonManifest,
  ComparisonRecord,
  ComparisonResult,
  ComparisonSectionSummary,
  SourceRef,
} from "@/client/types.gen";
import { Panel } from "@/components/common";
import { JsonDetails, Metadata } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { CodeDiff } from "@/components/code-editor";
import {
  comparisonCaution,
  comparisonReplayTarget,
  immutableReplayTarget,
  objectValue,
  sideTitle,
} from "@/lib/comparison-evidence";
import { recordedValue } from "@/lib/workbench-evidence";
import {
  type ComparisonSide,
  type WorkbenchSelection,
} from "@/lib/workbench-selection";
import { AnchoredSource } from "./anchored-source";
import { ComparisonVersion } from "./comparison-version";
import {
  ReplayTimestampCaveat,
  replayTimestampPresentation,
} from "./replay-timing";
const ComparisonChart = lazy(() => import("./comparison-chart"));
export function configurationValue(present: boolean, value: unknown): string {
  if (!present) return "Missing / not recorded";
  if (value === null) return "null (explicit recorded value)";
  return recordedValue(value);
}
export function ComparisonConfiguration({
  manifest,
}: {
  manifest: ComparisonManifest;
}) {
  return (
    <Panel title="1. Fixed inputs and execution configuration">
      <p className="notice" data-testid="comparison-basis">
        Inspect the complete basis before outcomes. Different data, universe,
        exact leaf windows, warmup, fees, fill rules, initialization/reset
        policy, market, dependencies/image or RNG can explain differences.
        Unknown fields do not match by default.
      </p>
      <div className="replay-table-scroll">
        <table
          className="replay-table"
          aria-label="Immutable configuration differences"
        >
          <thead>
            <tr>
              <th>Field</th>
              <th>Left</th>
              <th>Right</th>
              <th>Comparison</th>
            </tr>
          </thead>
          <tbody>
            {manifest.configuration_differences.map((field, index) => (
              <tr key={`${field.field}:${index}`}>
                <th>{field.field}</th>
                <td>{configurationValue(field.left_present, field.left)}</td>
                <td>{configurationValue(field.right_present, field.right)}</td>
                <td>{field.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Metadata
        values={{
          "Event namespace": manifest.namespace_status,
          "Namespace reason": manifest.namespace_reason,
          "Pairing fields (not date/symbol joins)": manifest.pairing_fields,
          "Left exact leaf window": manifest.left.window,
          "Right exact leaf window": manifest.right.window,
        }}
      />
      <p className="notice mt-3">
        Changed strategy code and parameters are the objects of study. Multiple
        changes or unknown fixed conditions are not a single-factor validation.
      </p>
    </Panel>
  );
}
export function ComparisonMechanisms({
  result,
  selection,
  update,
}: {
  result: ComparisonResult;
  selection: WorkbenchSelection;
  update: (patch: Partial<WorkbenchSelection>) => void;
}) {
  const manifest = result.manifest;
  const selected =
    selection.mechanism_index !== undefined
      ? manifest.mechanism_changes[selection.mechanism_index]
      : undefined;
  return (
    <Panel title="2. Mechanism summaries and immutable source diff">
      <div data-testid="comparison-mechanisms">
        <p className="notice">
          Descriptions summarize source changes, not runtime facts. Controlled,
          human-confirmed, inferred and unmapped relationships remain explicit.
          No summary establishes independent causal validation.
        </p>
        {(
          ["input", "filter", "scoring", "portfolio", "execution"] as const
        ).map((category) => {
          const changes = manifest.mechanism_changes
            .map((value, index) => ({ value, index }))
            .filter(({ value }) => value.category === category);
          return (
            <section
              key={category}
              className="mt-4"
              aria-label={`${category} mechanism changes`}
            >
              <h3 className="font-semibold">{category}</h3>
              {!changes.length && (
                <p className="muted text-sm">
                  No mapped rule summary for this category. This does not prove
                  the category is unchanged.
                </p>
              )}
              {changes.map(({ value, index }) => (
                <article
                  key={index}
                  className="replay-record"
                  data-selected={selection.mechanism_index === index}
                >
                  <p>
                    {value.rule_id || "Unmapped source change"} · {value.status}{" "}
                    · Mapping: {value.mapping}
                  </p>
                  {value.reason && <p className="notice">{value.reason}</p>}
                  <div className="comparison-sides">
                    {(["left", "right"] as const).map((side) => (
                      <section key={side}>
                        <h4>{sideTitle(side)}</h4>
                        <Metadata
                          values={
                            value[side] || {
                              State:
                                "No mapped rule; not an empty or false rule",
                            }
                          }
                        />
                        <AnchorLabel anchor={value[`${side}_source_ref`]} />
                      </section>
                    ))}
                  </div>
                  {(value.left_source_ref || value.right_source_ref) && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => update({ mechanism_index: index })}
                    >
                      Inspect exact source anchors {index + 1}
                    </Button>
                  )}
                </article>
              ))}
            </section>
          );
        })}
      </div>
      <Metadata
        values={{
          "Left code SHA-256": result.left_source.code_sha256,
          "Right code SHA-256": result.right_source.code_sha256,
          "Code changed": manifest.code_changed,
          "Left description SHA-256": result.left_source.spec_sha256,
          "Right description SHA-256": result.right_source.spec_sha256,
        }}
      />
      {result.left_source.spec_missing_reason && (
        <p className="notice">Left: {result.left_source.spec_missing_reason}</p>
      )}
      {result.right_source.spec_missing_reason && (
        <p className="notice">
          Right: {result.right_source.spec_missing_reason}
        </p>
      )}
      <CodeDiff
        original={result.left_source.code}
        modified={result.right_source.code}
        label="Immutable left and right source diff"
      />
      {selection.mechanism_index !== undefined && !selected && (
        <p role="alert" className="notice error">
          The selected mechanism summary is absent from this exact manifest. No
          other source anchor was selected.
        </p>
      )}
      {selected && (
        <div className="comparison-sides">
          {(["left", "right"] as const).map((side) => (
            <section
              key={side}
              aria-label={`${sideTitle(side)} selected source anchor`}
            >
              <h3>{sideTitle(side)} exact anchor</h3>
              {selected[`${side}_source_ref`] ? (
                <AnchoredSource
                  code={result[`${side}_source`].code}
                  codeSha256={result[`${side}_source`].code_sha256}
                  anchor={selected[`${side}_source_ref`]}
                />
              ) : (
                <p className="notice">
                  No corresponding source anchor. A nearby line was not
                  substituted.
                </p>
              )}
            </section>
          ))}
        </div>
      )}
    </Panel>
  );
}
function AnchorLabel({ anchor }: { anchor: SourceRef | null }) {
  return (
    <p className="text-xs break-all mt-2">
      {anchor
        ? `${anchor.file} · ${anchor.symbol} · lines ${anchor.line_start}–${anchor.line_end} · ${anchor.code_sha256}`
        : "Source anchor unmapped / unavailable"}
    </p>
  );
}
export function ComparisonOutcome({
  manifest,
}: {
  manifest: ComparisonManifest;
}) {
  return (
    <Panel title="3. Observed behavior and coverage">
      <ComparisonVersion version={manifest.algorithm_version} />
      <p className="notice" data-testid="comparison-outcome">
        {manifest.observed_behavior === "unchanged_observed"
          ? `${manifest.code_changed ? "Code changed, but" : "Source code is unchanged and"} no behavior difference was observed in the declared comparable sections and saved window. Result: unchanged_observed.`
          : manifest.observed_behavior === "changed"
            ? "Recorded behavior differs. Result: changed. Inspect exact events and both pre-states before attributing any mechanism."
            : "Insufficient coverage to determine unchanged behavior. Missing, unsupported or ambiguous records are not equal observations. Result: insufficient_coverage."}
      </p>
      {manifest.code_changed && manifest.observed_equal_sections.length > 0 && (
        <p className="notice mt-3" data-testid="comparison-scoped-unchanged">
          Code changed; these recorded sections are unchanged:{" "}
          {manifest.observed_equal_sections.join(", ")}. Full behavior remains
          unknown outside that named observed coverage. This is not diagnostic
          parity or causal validation.
        </p>
      )}
      <p className="notice mt-3">{comparisonCaution}</p>
      <div data-testid="comparison-coverage">
        <Metadata
          values={{
            "Equal observed sections only": manifest.observed_equal_sections,
            "Left trace state": manifest.left.trace_state,
            "Left coverage": manifest.left.trace_coverage ?? "Not collected",
            "Right trace state": manifest.right.trace_state,
            "Right coverage": manifest.right.trace_coverage ?? "Not collected",
            "Left retained capture scope":
              manifest.left.capture ??
              "Historical absence; no capture declaration",
            "Right retained capture scope":
              manifest.right.capture ??
              "Historical absence; no capture declaration",
          }}
        />
        {[
          ...manifest.left.trace_missing_reasons.map((v) => `Left: ${v}`),
          ...manifest.right.trace_missing_reasons.map((v) => `Right: ${v}`),
          ...manifest.warnings,
        ].map((reason, index) => (
          <p className="notice mt-2" key={index}>
            {reason}
          </p>
        ))}
        <div className="replay-table-scroll">
          <table
            className="replay-table"
            aria-label="Comparison section coverage"
          >
            <thead>
              <tr>
                <th>Section</th>
                <th>Left availability</th>
                <th>Right availability</th>
                <th>Observed equality</th>
                <th>
                  Rows / changed / left-only / right-only / ambiguous /
                  unavailable
                </th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(manifest.sections).map(([name, value]) => (
                <tr key={name}>
                  <th>{name}</th>
                  <td>
                    {value.left_availability}
                    <p>{value.left_reason}</p>
                  </td>
                  <td>
                    {value.right_availability}
                    <p>{value.right_reason}</p>
                  </td>
                  <td>{value.observed_equality}</td>
                  <td>
                    {[
                      value.rows,
                      value.changed,
                      value.left_only,
                      value.right_only,
                      value.ambiguous,
                      value.unavailable,
                    ].join(" / ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <JsonDetails
        title="Fixed field-specific comparison tolerances"
        value={{
          version: manifest.tolerance_version,
          fields: manifest.field_tolerances,
        }}
      />
    </Panel>
  );
}
export function ComparisonPrestates({
  result,
  event,
}: {
  result: ComparisonResult;
  event?: ComparisonRecord;
}) {
  return (
    <section
      data-testid="comparison-prestates"
      aria-label="Both decision pre-states"
      className="mt-4"
    >
      <h3 className="font-semibold">Both decision pre-states</h3>
      <p className="notice">{comparisonCaution}</p>
      <div className="comparison-sides">
        {(["left", "right"] as const).map((side) => {
          const value = event?.[side];
          const timestampEvidence = result[`${side}_timestamp_evidence`];
          const timing = replayTimestampPresentation(timestampEvidence);
          const target = event
            ? comparisonReplayTarget(result, side, event)
            : null;
          return (
            <section
              key={side}
              className="replay-record"
              data-testid={`comparison-prestate-${side}`}
            >
              <h4>{sideTitle(side)} pre-state</h4>
              {!event ? (
                <p className="notice">
                  Select an explicit event pair to inspect this exact decision
                  pre-state.
                </p>
              ) : (
                <>
                  <Metadata
                    values={{
                      "Decision ID":
                        value?.decision_id ?? "No paired event on this side",
                      "Evidence state": event[`${side}_state`],
                      [timing.cutoffLabel]:
                        value?.information_cutoff ?? "Not recorded",
                      [timing.decisionLabel]:
                        value?.decision_time ?? "Not recorded",
                      "Market timezone":
                        value?.market_timezone ?? "Not recorded",
                    }}
                  />
                  <ReplayTimestampCaveat context={timestampEvidence} />
                  {value?.pre_state !== undefined &&
                  value.pre_state !== null ? (
                    <Metadata
                      values={
                        objectValue(value.pre_state) || {
                          "Recorded pre-state": value.pre_state,
                        }
                      }
                    />
                  ) : (
                    <p className="notice">
                      {typeof value?.pre_state_missing_reason === "string"
                        ? value.pre_state_missing_reason
                        : "Pre-state not collected or this event is unmatched. Cash and holdings are not assumed to be zero."}
                    </p>
                  )}
                  {target ? (
                    <Link {...target} className="text-link">
                      Open {sideTitle(side)} immutable replay
                    </Link>
                  ) : (
                    <p className="text-xs muted">
                      No exact decision link; no date/symbol substitute was
                      inferred.
                    </p>
                  )}
                </>
              )}
            </section>
          );
        })}
      </div>
    </section>
  );
}
export function ComparisonAvailability({
  value,
}: {
  value: ComparisonSectionSummary;
}) {
  return (
    <div className="notice">
      <p>{value.definition}</p>
      <p>
        Left: {value.left_availability}
        {value.left_reason ? ` · ${value.left_reason}` : ""}. Right:{" "}
        {value.right_availability}
        {value.right_reason ? ` · ${value.right_reason}` : ""}. Observed
        equality: {value.observed_equality}.
      </p>
      <Metadata
        values={{
          "Left observation scope": value.left_scope,
          "Right observation scope": value.right_scope,
          "Equality scope": value.equality_scope,
        }}
      />
    </div>
  );
}
export function ComparisonRecords({
  rows,
  result,
  selectedEvent,
  selectedPair,
  onPair,
}: {
  rows: ComparisonRecord[];
  result: ComparisonResult;
  selectedEvent?: ComparisonRecord;
  selectedPair?: string;
  onPair: (id: string) => void;
}) {
  return (
    <>
      {rows.length > 0 && (
        <Suspense
          fallback={<p role="status">Loading bounded comparison plot…</p>}
        >
          <ComparisonChart rows={rows} onPair={onPair} />
        </Suspense>
      )}
      {!rows.length && (
        <p className="notice">
          No rows in this bounded selection. Consult both availability
          declarations; an empty page is not evidence of zero positions or equal
          behavior.
        </p>
      )}
      {rows.map((row) => (
        <article
          key={row.row_id}
          className="replay-record"
          data-testid={`comparison-record-${row.row_id}`}
          data-selected={
            row.event_pair_id === selectedPair && Boolean(selectedPair)
          }
        >
          <h3 className="font-semibold">
            {row.section} · {row.status} ·{" "}
            {row.instrument_id || "portfolio / whole event"}
          </h3>
          <Metadata
            values={{
              "Exact row": row.row_id,
              "Event pair": row.event_pair_id,
              Session: row.session_date,
              Stage: row.stage_id,
              "Match basis": row.match_basis,
              Ambiguous: row.ambiguous,
            }}
          />
          {row.missing_reason && <p className="notice">{row.missing_reason}</p>}
          {row.event_pair_id ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => onPair(row.event_pair_id!)}
            >
              Select event pair {row.event_pair_id}
            </Button>
          ) : (
            <p className="notice">
              No explicit event pair. A timestamp or symbol does not establish
              an order or decision association.
            </p>
          )}
          <div className="comparison-sides">
            {(["left", "right"] as const).map((side) => (
              <section key={side}>
                <h4>
                  {sideTitle(side)} · {row[`${side}_state`]}
                </h4>
                <EvidenceItems
                  row={row}
                  side={side}
                  result={result}
                  event={
                    row.section === "events"
                      ? row
                      : selectedEvent?.event_pair_id === row.event_pair_id
                        ? selectedEvent
                        : undefined
                  }
                />
                {["orders", "fills"].includes(row.section) && (
                  <TypedExecutionAssociations
                    result={result}
                    side={side}
                    row={row}
                    onPair={onPair}
                  />
                )}
              </section>
            ))}
          </div>
        </article>
      ))}
    </>
  );
}
function EvidenceItems({
  row,
  side,
  result,
  event,
}: {
  row: ComparisonRecord;
  side: ComparisonSide;
  result: ComparisonResult;
  event?: ComparisonRecord;
}) {
  const value = row[side];
  if (value === null)
    return (
      <p className="notice">
        {row[`${side}_state`] === "observed_absence"
          ? "Explicit observed absence on this side; not a zero-valued record."
          : `Evidence unavailable: ${row[`${side}_state`]}. No empty or zero record was invented.`}
      </p>
    );
  const items = Array.isArray(value.items) ? value.items : null;
  if (!items)
    return (
      <>
        <Metadata
          values={Object.fromEntries(
            Object.entries(value).filter(([key]) => key !== "pre_state"),
          )}
        />
        <JsonDetails
          title={`${sideTitle(side)} exact saved record`}
          value={value}
        />
      </>
    );
  return (
    <>
      {items.map((unknownItem, index) => {
        const item = objectValue(unknownItem);
        const record = objectValue(item?.record);
        const ordinal = item?.ordinal;
        const target =
          event && record
            ? exactRecordTarget(result, side, event, row, item!, record)
            : null;
        return (
          <div key={index} className="replay-record">
            <p className="text-xs">
              Saved record ordinal:{" "}
              {typeof ordinal === "number" ? ordinal : "Not recorded"}
            </p>
            <Metadata values={record || { "Saved evidence": unknownItem }} />
            {target && (
              <Link {...target} className="text-link">
                Open {sideTitle(side)} {row.section} record{" "}
                {typeof ordinal === "number"
                  ? ordinal
                  : String(record?.logical_order_id)}
              </Link>
            )}
            {!target && !["orders", "fills"].includes(row.section) && (
              <p className="text-xs muted">
                No exact replay-record target is available for this record. No
                first row or proximity-based order link was substituted.
              </p>
            )}
          </div>
        );
      })}
    </>
  );
}
export function exactRecordTarget(
  result: ComparisonResult,
  side: ComparisonSide,
  event: ComparisonRecord,
  row: ComparisonRecord,
  item: Record<string, unknown>,
  record: Record<string, unknown>,
) {
  if (!row.event_pair_id || event.event_pair_id !== row.event_pair_id)
    return null;
  const stage =
    typeof item.stage_id === "string" ? item.stage_id : row.stage_id;
  const instrument =
    typeof record.instrument_id === "string"
      ? record.instrument_id
      : row.instrument_id;
  const ordinal = item.ordinal;
  if (["candidates", "features", "predicates"].includes(row.section)) {
    if (!stage || !Number.isSafeInteger(ordinal) || (ordinal as number) < 0)
      return null;
    const kind =
      row.section === "candidates"
        ? "candidate"
        : row.section === "features"
          ? "feature"
          : "predicate";
    const id =
      kind === "candidate"
        ? instrument
        : record[kind === "feature" ? "feature_id" : "condition_id"];
    if (typeof id !== "string" || !id) return null;
    return comparisonReplayTarget(result, side, event, {
      record_kind: kind,
      record_id: id,
      record_ordinal: ordinal as number,
      stage_id: stage,
      instrument_id: instrument ?? undefined,
    });
  }
  return null;
}

export function explicitAssociationTarget(
  result: ComparisonResult,
  association: ComparisonAssociation,
) {
  if (
    !["left", "right"].includes(association.side) ||
    !association.decision_id ||
    !association.stage_id ||
    !association.logical_order_id ||
    !association.instrument_id ||
    !Number.isSafeInteger(association.link_ordinal) ||
    association.link_ordinal < 0
  )
    return null;
  // This is an explicit same-side saved graph edge, even when the cross-run
  // event cannot be paired. Child/fill IDs are displayed, never joined by time.
  return immutableReplayTarget(
    result,
    association.side,
    association.decision_id,
    {
      record_kind: "order",
      record_id: association.logical_order_id,
      stage_id: association.stage_id,
      instrument_id: association.instrument_id,
      record_ordinal: association.link_ordinal,
    },
  );
}
function TypedExecutionAssociations({
  result,
  side,
  row,
  onPair,
}: {
  result: ComparisonResult;
  side: ComparisonSide;
  row: ComparisonRecord;
  onPair: (id: string) => void;
}) {
  const associations =
    row.associations?.filter((value) => value.side === side) || [];
  return (
    <section aria-label={`${sideTitle(side)} explicit execution associations`}>
      {!associations.length && (
        <p className="notice">
          No saved execution-to-decision association. No order link is inferred
          from timestamp, symbol, or row position.
        </p>
      )}
      {associations.map((association, index) => {
        const target = explicitAssociationTarget(result, association);
        return (
          <div key={index} className="replay-record">
            <Metadata
              values={{
                "Saved decision ID": association.decision_id,
                "Logical order ID": association.logical_order_id,
                "Recorded link ordinal": association.link_ordinal,
                "Event pair": association.event_pair_id,
                "Source event paired": association.source_event_paired,
                "Explicit child order IDs": association.child_order_ids,
                "Explicit fill IDs": association.fill_ids,
                Stage: association.stage_id,
              }}
            />
            {target ? (
              <Link {...target} className="text-link">
                Open {sideTitle(side)} {row.section} association {index + 1}
              </Link>
            ) : (
              <p className="notice">
                This saved association is incomplete. No replacement decision or
                order link was invented.
              </p>
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={() => onPair(association.event_pair_id)}
            >
              Inspect {sideTitle(side)} association event {index + 1}
            </Button>
          </div>
        );
      })}
    </section>
  );
}
