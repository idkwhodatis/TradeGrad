import { useEffect, useState } from "react";
import {
  captureBudgetFields,
  captureSummary,
  parseCaptureScope,
  researchCaptureSchema,
  type ResearchCapture,
} from "@/lib/research-capture";
import { Field } from "./data-common";
import { Input } from "./ui/input";

function CaptureScopeInput({
  id,
  value,
  onChange,
}: {
  id: string;
  value: string[] | undefined;
  onChange: (ids: string[]) => void;
}) {
  const [text, setText] = useState(() => value?.join("\n") ?? "");
  const key = JSON.stringify(value ?? []);
  useEffect(() => {
    // Keep an unfinished comma/newline while typing. External resets still win.
    setText((current) =>
      JSON.stringify(parseCaptureScope(current)) === key
        ? current
        : (JSON.parse(key) as string[]).join("\n"),
    );
  }, [key]);
  return (
    <textarea
      id={id}
      rows={3}
      value={text}
      aria-describedby={`${id}-help`}
      onChange={(event) => {
        setText(event.target.value);
        onChange(parseCaptureScope(event.target.value));
      }}
    />
  );
}

export function ResearchCaptureControls({
  value,
  onChange,
  disabled,
  prefix = "research_capture",
  portable = false,
}: {
  value: ResearchCapture | null | undefined;
  onChange: (capture: ResearchCapture | undefined) => void;
  disabled?: boolean;
  prefix?: string;
  portable?: boolean;
}) {
  const active = value != null && value.mode !== "off";
  const parsed = value == null ? null : researchCaptureSchema.safeParse(value);
  const errors = parsed && !parsed.success ? parsed.error.issues : [];
  function patch(change: Partial<ResearchCapture>) {
    if (value) onChange({ ...value, ...change });
  }
  function optional<K extends keyof ResearchCapture>(
    key: K,
    next: ResearchCapture[K] | undefined,
  ) {
    if (!value) return;
    const capture = { ...value };
    if (next === undefined) delete capture[key];
    else capture[key] = next;
    onChange(capture);
  }
  return (
    <fieldset
      disabled={disabled}
      className="stack"
      data-testid="research-capture-controls"
    >
      <legend>Research observation capture</legend>
      <p className="notice">
        These settings only limit saved observations. The full pinned dataset,
        universe, ranking candidate pool, execution windows and warm-up remain
        unchanged. An observation window never changes the state-reconstruction
        start or authorizes a holdout evaluation.
      </p>
      <Field
        id={`${prefix}.mode`}
        label="Observation capture mode"
        help="Default leaves research_capture omitted for historical request compatibility. Summary saves available execution and candidate summaries; research requests supported internal details. Neither creates missing instrumentation."
      >
        <select
          id={`${prefix}.mode`}
          aria-describedby={`${prefix}.mode-help`}
          value={value?.mode ?? ""}
          onChange={(event) => {
            const mode = event.target.value as ResearchCapture["mode"] | "";
            if (!mode) onChange(undefined);
            else if (mode === "off") onChange({ mode });
            else onChange({ ...value, mode });
          }}
        >
          <option value="">Default · summary (no override)</option>
          <option value="off">Off · no research observations</option>
          <option value="summary">
            Summary · available execution summaries
          </option>
          <option value="research">
            Research · supported detailed observations
          </option>
        </select>
      </Field>
      <p className="muted text-xs" data-testid="research-capture-summary">
        {captureSummary(value)}
      </p>
      <fieldset disabled={!active} className="stack">
        <label className="flex items-center gap-2">
          <input
            id={`${prefix}.observation_window`}
            type="checkbox"
            checked={Boolean(value?.observation_window)}
            onChange={(event) =>
              optional(
                "observation_window",
                event.target.checked ? { start: "", end: "" } : undefined,
              )
            }
          />
          Limit saved observations to a date window
        </label>
        {value?.observation_window && (
          <div className="form-row">
            {(["start", "end"] as const).map((edge) => (
              <Field
                key={edge}
                id={`${prefix}.observation_window.${edge}`}
                label={`Observation ${edge} · ${edge === "start" ? "inclusive" : "exclusive"}`}
              >
                <Input
                  id={`${prefix}.observation_window.${edge}`}
                  type="date"
                  value={value.observation_window![edge]}
                  onChange={(event) =>
                    patch({
                      observation_window: {
                        ...value.observation_window!,
                        [edge]: event.target.value,
                      },
                    })
                  }
                />
              </Field>
            ))}
          </div>
        )}
        <div className="form-row">
          {(["stage_ids", "instrument_ids"] as const).map((key) => (
            <Field
              key={key}
              id={`${prefix}.${key}`}
              label={
                key === "stage_ids"
                  ? "Observation stage IDs"
                  : "Observation instrument IDs"
              }
              help={`Comma- or newline-separated exact IDs; blank means all. At most ${key === "stage_ids" ? "100 stages" : "10,000 instruments"}, 200 characters per ID. Unknown IDs produce missing observations, not a different strategy universe.`}
            >
              <CaptureScopeInput
                id={`${prefix}.${key}`}
                value={value?.[key]}
                onChange={(ids) => optional(key, ids.length ? ids : undefined)}
              />
            </Field>
          ))}
        </div>
        <div className="form-grid">
          {captureBudgetFields.map(({ key, label, min, max, defaultValue }) => (
            <Field
              key={key}
              id={`${prefix}.${key}`}
              label={label}
              help={`Optional integer from ${min} to ${max}. Blank uses server default ${defaultValue}; it is not inserted into the request.`}
            >
              <Input
                id={`${prefix}.${key}`}
                type="number"
                min={min}
                max={max}
                step={1}
                value={value?.[key] ?? ""}
                placeholder={String(defaultValue)}
                aria-describedby={`${prefix}.${key}-help`}
                onChange={(event) =>
                  optional(
                    key,
                    event.target.value === ""
                      ? undefined
                      : event.target.valueAsNumber,
                  )
                }
              />
            </Field>
          ))}
        </div>
      </fieldset>
      <p className="muted text-xs">
        Saved scope and capacity limits are pinned in the trace manifest.
        Truncation, collection errors and unavailable stages remain explicit
        partial or missing coverage, never false conditions or zero positions.
        {portable &&
          " Portable internals are available only where the actual policy protocol exposes them; the 100-listing ABI cap is unchanged."}
      </p>
      {errors.length > 0 && (
        <ul className="notice error" role="alert">
          {errors.map((error, index) => (
            <li key={index}>
              <a
                href={`#${prefix}.${error.path[0] === "stage_ids" || error.path[0] === "instrument_ids" ? error.path[0] : error.path.join(".")}`}
              >
                {error.path.join(".")}: {error.message}
              </a>
            </li>
          ))}
        </ul>
      )}
    </fieldset>
  );
}
