import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type {
  IndependentEvaluationPublic,
  IndependentResultPin,
  IndependentResultQuery,
} from "@/client/types.gen";
import { ErrorState, Loading, Panel } from "@/components/common";
import { JsonDetails, Metadata, Pagination } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { asRecord, valueText } from "@/lib/data-utils";
import { independentResultOptions } from "@/lib/verification-api";
export function independentResultMatches(
  result: IndependentResultQuery,
  job: IndependentEvaluationPublic,
  pin: IndependentResultPin,
  section: string,
  offset: number,
) {
  return (
    result.evaluation_id === pin.evaluation_id &&
    result.execution_attempt_id === pin.execution_attempt_id &&
    result.run_id === job.id &&
    result.purpose === job.purpose &&
    result.artifact_sha256 === pin.result_manifest_sha256 &&
    result.evaluation_mode === job.config.evaluation_mode &&
    result.reward_status === "not_applicable" &&
    result.section === section &&
    result.offset === offset &&
    result.limit === 25 &&
    result.rows.length <= 25
  );
}
export function IndependentResults({
  owner,
  job,
  pin,
}: {
  owner: string;
  job: IndependentEvaluationPublic;
  pin: IndependentResultPin;
}) {
  const native = job.config.evaluation_mode === "native_akquant";
  const [section, setSection] = useState(native ? "equity" : "account_series"),
    [offset, setOffset] = useState(0);
  const query = useQuery(independentResultOptions(owner, pin, section, offset));
  const result =
    query.data &&
    !query.error &&
    independentResultMatches(query.data, job, pin, section, offset)
      ? query.data
      : undefined;
  return (
    <Panel title="Actual independent economic observations">
      <p className="notice">
        These are newly executed observations, not an optimizer score. Valid
        no-trade or no-closed-trade executions keep observed tables and
        explicitly unavailable metrics. Failure is never converted into zero
        profit. Values retain their persisted units; no missing number is
        synthesized.
      </p>
      <div className="flex flex-wrap gap-2">
        {(native
          ? [
              "equity",
              "drawdown",
              "cash",
              "positions",
              "orders",
              "fills",
              "closed_trades",
            ]
          : [
              "account_series",
              "positions",
              "orders",
              "fills",
              "closed_trades",
              "logical_orders",
            ]
        ).map((value) => (
          <Button
            key={value}
            size="sm"
            variant={value === section ? "default" : "outline"}
            aria-pressed={value === section}
            onClick={() => {
              setSection(value);
              setOffset(0);
            }}
          >
            {value.replaceAll("_", " ")}
          </Button>
        ))}
      </div>
      {query.isPending && (
        <Loading text="Reading exact persisted independent result…" />
      )}
      {query.error && (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      )}
      {query.data && !query.error && !result && (
        <ErrorState
          error={
            new Error(
              "The independent economic response differs from the exact run, physical artifact or selected page. It was not displayed.",
            )
          }
        />
      )}
      {result && (
        <>
          <Metadata
            values={{
              "Exact result SHA-256": result.artifact_sha256,
              "Result trust": result.result_trust,
              "Optimizer reward": result.reward_status,
            }}
          />
          <Metadata
            values={Object.fromEntries(
              Object.entries(result.metrics).map(([key, value]) => {
                const detail = result.metric_details.find(
                  (item) => item.name === key,
                );
                return [
                  detail?.unit ? `${key} [${String(detail.unit)}]` : key,
                  value ??
                    `Unavailable, not zero${detail?.unavailable_reason ? `: ${String(detail.unavailable_reason)}` : ""}`,
                ];
              }),
            )}
          />
          <JsonDetails
            title="Persisted metric definitions and missing reasons"
            value={result.metric_details}
          />
          {result.rows.length === 0 ? (
            <p className="notice">
              No observed rows in this {section} table. This is not a failed run
              promoted to zero profit.
            </p>
          ) : (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    {result.columns.map((column) => (
                      <th key={column}>{column}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.rows.map((row, index) => (
                    <tr key={offset + index}>
                      {result.columns.map((column, columnIndex) => (
                        <td key={column}>
                          {valueText(
                            Array.isArray(row)
                              ? row[columnIndex]
                              : asRecord(row)[column],
                          )}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <Pagination
            offset={offset}
            limit={25}
            count={result.total}
            busy={query.isFetching}
            onPage={setOffset}
          />
          <ul className="notice">
            {result.warnings.map((warning, index) => (
              <li key={index}>{warning}</li>
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
}
