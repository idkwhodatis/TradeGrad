import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ErrorState, Loading, Panel } from "@/components/common";
import { Button } from "@/components/ui/button";
import { currentUserOptions } from "@/lib/api";
import {
  formatDuration,
  observationStale,
  operationsOptions,
} from "@/lib/operations";
import { formatDate } from "@/lib/format";

const fraction = (value: number | null | undefined) =>
  value == null
    ? "Unavailable: no completed imports"
    : `${(value * 100).toFixed(1)}%`;
const bytes = (value: number | null | undefined) =>
  value == null ? "Unavailable" : `${(value / 1024 ** 3).toFixed(2)} GiB`;

export function OperationsPanel() {
  const user = useQuery(currentUserOptions);
  const [scope, setScope] = useState<"owner" | "deployment">("owner");
  const [days, setDays] = useState(7);
  const [now, setNow] = useState(Date.now());
  const query = useQuery(operationsOptions(scope, days));
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(timer);
  }, []);
  const data = query.data;
  return (
    <Panel
      title="Operational observations"
      action={
        <Button
          variant="outline"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
        >
          Refresh observations
        </Button>
      }
    >
      <div className="flex gap-4 flex-wrap mb-4">
        <label>
          Observation scope{" "}
          <select
            aria-label="Observation scope"
            value={scope}
            onChange={(event) =>
              setScope(event.target.value as "owner" | "deployment")
            }
          >
            <option value="owner">My resources</option>
            {user.data?.is_superuser && (
              <option value="deployment">Deployment (administrator)</option>
            )}
          </select>
        </label>
        <label>
          Observation window{" "}
          <select
            aria-label="Observation window"
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
          >
            <option value={1}>1 day</option>
            <option value={7}>7 days</option>
            <option value={31}>31 days</option>
          </select>
        </label>
      </div>
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : (
        data && (
          <>
            <p className="text-xs muted">
              Observed {formatDate(data.observed_at)} · window starts{" "}
              {formatDate(data.window_start)} · {data.scope} scope
            </p>
            {observationStale(
              data.observed_at,
              data.observation_ttl_seconds ?? 60,
              now,
            ) && (
              <p role="status">
                Stale observation. Refresh before relying on these figures.
              </p>
            )}
            <dl className="details">
              <div>
                <dt>Queued runs / requested imports</dt>
                <dd>
                  {data.queued_runs} / {data.queued_imports}
                  {data.queue_counts_truncated &&
                    " (lower bounds; queue sample capped)"}
                </dd>
              </div>
              <div>
                <dt>Active runs / claimed imports</dt>
                <dd>
                  {data.active_runs} / {data.active_imports}
                  {data.queue_counts_truncated &&
                    " (lower bounds; sample capped)"}
                </dd>
              </div>
              <div>
                <dt>Current queue age</dt>
                <dd>{formatDuration(data.current_queue_age)}</dd>
              </div>
              <div>
                <dt>Queue wait (started attempts)</dt>
                <dd>{formatDuration(data.queue_wait)}</dd>
              </div>
              <div>
                <dt>Completed attempt runtime</dt>
                <dd>
                  {formatDuration(data.attempt_runtime)} ·{" "}
                  {data.attempt_runtime.unavailable_count}{" "}
                  incomplete/unavailable
                </dd>
              </div>
              <div>
                <dt>Recorded cancellation latency</dt>
                <dd>
                  {formatDuration(data.cancellation_latency)} ·{" "}
                  {data.cancellation_latency.unavailable_count}{" "}
                  unconfirmed/incomplete
                </dd>
              </div>
              <div>
                <dt>Import failure rate</dt>
                <dd>
                  {fraction(data.imports.failure_fraction)} · failed{" "}
                  {data.imports.failed}, partial {data.imports.partial}, ready{" "}
                  {data.imports.ready}, cancelled {data.imports.cancelled}
                </dd>
              </div>
              <div>
                <dt>Failed or partial rate</dt>
                <dd>{fraction(data.imports.unsuccessful_fraction)}</dd>
              </div>
              <div>
                <dt>Model usage</dt>
                <dd>
                  {data.model_usage.calls} calls · input{" "}
                  {data.model_usage.input_tokens ?? "unknown"} / output{" "}
                  {data.model_usage.output_tokens ?? "unknown"} tokens ·{" "}
                  {data.model_usage.unknown_usage_calls} unknown-usage calls
                </dd>
              </div>
              <div>
                <dt>Local charged/reserved tokens</dt>
                <dd>
                  {data.model_usage.charged_tokens ?? "unknown"} · not currency
                  cost
                </dd>
              </div>
              <div>
                <dt>Storage filesystem</dt>
                <dd>
                  {data.disk.state === "observed"
                    ? `${bytes(data.disk.available_bytes)} available / ${bytes(data.disk.free_bytes)} free / ${bytes(data.disk.capacity_bytes)} capacity`
                    : data.disk.state === "restricted"
                      ? "Administrator-only aggregate; no owner storage size inferred"
                      : "Unavailable; storage filesystem could not be observed"}
                </dd>
              </div>
            </dl>
            {(data.attempts_truncated ||
              data.imports.truncated ||
              data.model_usage.truncated) && (
              <p role="status">
                Bounded sample reached: latest {data.sample_limit} records per
                category. These are not whole-population rates or totals.
              </p>
            )}
            <details className="mt-3 text-xs muted">
              <summary>Measurement definitions and limits</summary>
              {data.notes.map((note) => (
                <p className="mt-2" key={note}>
                  {note}
                </p>
              ))}
            </details>
          </>
        )
      )}
    </Panel>
  );
}
