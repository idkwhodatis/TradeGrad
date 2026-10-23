import { Field } from "@/components/data-common";
import { Input } from "@/components/ui/input";
import { label } from "@/lib/format";

export type TableFilters = {
  sort_by?: string;
  sort_direction?: "asc" | "desc";
  side?: "buy" | "sell";
  start?: string;
  end?: string;
  listing_id?: string;
};
export function ResultTableControls({
  columns,
  section,
  value,
  onChange,
  dates,
  listings = [],
}: {
  columns: string[];
  section: string;
  value: TableFilters;
  onChange: (value: TableFilters) => void;
  dates?: { start: string; end: string };
  listings?: { listing_id: string; symbol: string }[];
}) {
  const update = (patch: TableFilters) => onChange({ ...value, ...patch });
  const transactions = [
    "positions",
    "orders",
    "fills",
    "closed_trades",
    "logical_orders",
  ].includes(section);
  return (
    <div className="form-grid mt-4">
      <Field id="result-sort-column" label="Server sort column">
        <select
          id="result-sort-column"
          value={value.sort_by || ""}
          onChange={(event) =>
            update({
              sort_by: event.target.value || undefined,
              sort_direction: "asc",
            })
          }
        >
          <option value="">Persisted order</option>
          {columns.map((name) => (
            <option value={name} key={name}>
              {label(name)}
            </option>
          ))}
        </select>
      </Field>
      <Field id="result-sort-direction" label="Server sort direction">
        <select
          id="result-sort-direction"
          disabled={!value.sort_by}
          value={value.sort_direction || "asc"}
          onChange={(event) =>
            update({ sort_direction: event.target.value as "asc" | "desc" })
          }
        >
          <option value="asc">Ascending</option>
          <option value="desc">Descending</option>
        </select>
      </Field>
      {["orders", "fills", "logical_orders"].includes(section) && (
        <Field id="result-side" label="Execution side filter">
          <select
            id="result-side"
            value={value.side || ""}
            onChange={(event) =>
              update({
                side: (event.target.value || undefined) as TableFilters["side"],
              })
            }
          >
            <option value="">All sides</option>
            <option value="buy">Buy</option>
            <option value="sell">Sell</option>
          </select>
        </Field>
      )}
      {dates && section !== "native_metrics" && (
        <>
          <Field id="result-date-start" label="Table start date · inclusive">
            <Input
              type="date"
              id="result-date-start"
              min={dates.start}
              max={dates.end}
              value={value.start || ""}
              onChange={(event) =>
                update({ start: event.target.value || undefined })
              }
            />
          </Field>
          <Field id="result-date-end" label="Table end date · exclusive">
            <Input
              type="date"
              id="result-date-end"
              min={value.start || dates.start}
              max={dates.end}
              value={value.end || ""}
              onChange={(event) =>
                update({ end: event.target.value || undefined })
              }
            />
          </Field>
        </>
      )}
      {transactions && listings.length > 0 && (
        <Field id="result-listing" label="Table listing filter">
          <select
            id="result-listing"
            value={value.listing_id || ""}
            onChange={(event) =>
              update({ listing_id: event.target.value || undefined })
            }
          >
            <option value="">All pinned listings</option>
            {listings.map((item) => (
              <option key={item.listing_id} value={item.listing_id}>
                {item.symbol}
              </option>
            ))}
          </select>
        </Field>
      )}
    </div>
  );
}
