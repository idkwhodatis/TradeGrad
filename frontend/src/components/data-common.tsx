import type { ReactNode } from "react";
import { Info } from "lucide-react";
import { valueText } from "@/lib/data-utils";
import { Button } from "@/components/ui/button";

export function DataNotice() {
  return (
    <div className="notice">
      <Info />
      <p>
        Immutable research snapshots, not a live trading feed. Fixture
        verification is not live-provider verification. Unknown price
        adjustments and unverified point-in-time membership remain explicitly
        unknown.
      </p>
    </div>
  );
}

export function Metadata({ values }: { values: Record<string, unknown> }) {
  return (
    <dl className="data-metadata">
      {Object.entries(values).map(([name, value]) => (
        <div key={name}>
          <dt>{name}</dt>
          <dd>{valueText(value)}</dd>
        </div>
      ))}
    </dl>
  );
}
export function JsonDetails({
  title,
  value,
}: {
  title: string;
  value: unknown;
}) {
  return (
    <details className="mt-4">
      <summary className="text-link cursor-pointer">{title}</summary>
      <pre className="preview mt-3" tabIndex={0}>
        {JSON.stringify(value, null, 2)}
      </pre>
    </details>
  );
}
export function DataStatus({ status }: { status: string }) {
  return (
    <span className={`status status-${status}`} data-testid="import-status">
      {status.replaceAll("_", " ")}
    </span>
  );
}
export function Field({
  label,
  id,
  help,
  children,
}: {
  label: string;
  id: string;
  help?: string;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children}
      {help && <small id={`${id}-help`}>{help}</small>}
    </div>
  );
}
export function Pagination({
  offset,
  limit,
  count,
  busy,
  onPage,
}: {
  offset: number;
  limit: number;
  count: number;
  busy?: boolean;
  onPage: (offset: number) => void;
}) {
  return (
    <div className="data-pagination">
      <span>
        {count
          ? `${offset + 1}–${Math.min(offset + limit, count)} of ${count}`
          : "No records"}{" "}
        · {limit} per request
      </span>
      <div className="actions">
        <Button
          variant="outline"
          size="sm"
          disabled={!offset || busy}
          onClick={() => onPage(Math.max(0, offset - limit))}
        >
          Previous page
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={offset + limit >= count || busy}
          onClick={() => onPage(offset + limit)}
        >
          Next page
        </Button>
      </div>
    </div>
  );
}
