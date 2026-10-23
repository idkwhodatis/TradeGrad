import { useState } from "react";
import { Field } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { label } from "@/lib/format";

export type NamedListFilters = {
  sort_by?: string;
  sort_direction?: "asc" | "desc";
  search?: string;
  kind?: "seed" | "generated" | "repair";
  min_iteration?: number;
  mic?: string;
  currency?: string;
};
export function NamedListControls({
  kind,
  value,
  onChange,
}: {
  kind: "candidate" | "listing";
  value: NamedListFilters;
  onChange: (next: NamedListFilters) => void;
}) {
  const [draft, setDraft] = useState(value);
  const title = kind === "candidate" ? "Candidate" : "Listing";
  const sorts =
    kind === "candidate"
      ? [
          "created_at",
          "candidate_key",
          "generation",
          "iteration",
          "repair_index",
          "kind",
        ]
      : ["symbol", "mic", "currency", "created_at"];
  return (
    <div
      className="stack"
      role="group"
      aria-label={`${title} server query controls`}
    >
      <div className="form-grid">
        <Field
          id={`${kind}-search`}
          label={`${title} search`}
          help="Literal text search on the server, before pagination. No wildcard or SQL expressions."
        >
          <Input
            id={`${kind}-search`}
            maxLength={128}
            value={draft.search || ""}
            onChange={(event) =>
              setDraft({ ...draft, search: event.target.value || undefined })
            }
          />
        </Field>
        <Field id={`${kind}-sort`} label={`${title} server sort`}>
          <select
            id={`${kind}-sort`}
            value={draft.sort_by || sorts[0]}
            onChange={(event) =>
              setDraft({ ...draft, sort_by: event.target.value })
            }
          >
            {sorts.map((name) => (
              <option value={name} key={name}>
                {label(name)}
              </option>
            ))}
          </select>
        </Field>
        <Field id={`${kind}-direction`} label={`${title} sort direction`}>
          <select
            id={`${kind}-direction`}
            value={draft.sort_direction || "asc"}
            onChange={(event) =>
              setDraft({
                ...draft,
                sort_direction: event.target.value as "asc" | "desc",
              })
            }
          >
            <option value="asc">Ascending</option>
            <option value="desc">Descending</option>
          </select>
        </Field>
        {kind === "candidate" ? (
          <>
            <Field id="candidate-kind" label="Candidate kind">
              <select
                id="candidate-kind"
                value={draft.kind || ""}
                onChange={(event) =>
                  setDraft({
                    ...draft,
                    kind: (event.target.value ||
                      undefined) as NamedListFilters["kind"],
                  })
                }
              >
                <option value="">All kinds</option>
                {["seed", "generated", "repair"].map((name) => (
                  <option key={name} value={name}>
                    {label(name)}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              id="candidate-min-iteration"
              label="Minimum candidate iteration"
            >
              <Input
                id="candidate-min-iteration"
                type="number"
                min={0}
                max={100000}
                value={draft.min_iteration ?? ""}
                onChange={(event) =>
                  setDraft({
                    ...draft,
                    min_iteration: event.target.value
                      ? Number(event.target.value)
                      : undefined,
                  })
                }
              />
            </Field>
          </>
        ) : (
          <>
            <Field id="listing-mic" label="Listing MIC filter">
              <Input
                id="listing-mic"
                maxLength={4}
                value={draft.mic || ""}
                onChange={(event) =>
                  setDraft({ ...draft, mic: event.target.value || undefined })
                }
              />
            </Field>
            <Field id="listing-currency" label="Listing currency filter">
              <Input
                id="listing-currency"
                maxLength={16}
                value={draft.currency || ""}
                onChange={(event) =>
                  setDraft({
                    ...draft,
                    currency: event.target.value || undefined,
                  })
                }
              />
            </Field>
          </>
        )}
      </div>
      <div className="actions">
        <Button type="button" variant="outline" onClick={() => onChange(draft)}>
          Apply {kind} query
        </Button>
        <Button
          type="button"
          variant="ghost"
          onClick={() => {
            setDraft({});
            onChange({});
          }}
        >
          Reset {kind} query
        </Button>
      </div>
      <p className="text-xs muted">
        Server-filtered count and stable ID ordering. Existing selections stay
        bound to their original identities across queries.
      </p>
    </div>
  );
}
