import { z } from "zod";

// Authoring validation mirrors CaptureConfig. No defaults are materialized in
// the request: omitted settings retain the historical immutable wire shape.
export const captureBudgetFields = [
  {
    key: "max_decisions",
    label: "Maximum saved decisions",
    min: 1,
    max: 1000000,
    defaultValue: 10000,
  },
  {
    key: "max_detail_rows",
    label: "Maximum saved detail rows",
    min: 1,
    max: 20000000,
    defaultValue: 2000000,
  },
  {
    key: "max_uncompressed_bytes",
    label: "Maximum uncompressed capture bytes",
    min: 1024,
    max: 1073741824,
    defaultValue: 268435456,
  },
] as const;

const scope = (max: number) =>
  z
    .array(
      z
        .string()
        .min(1)
        .max(200)
        .refine((id) => id.trim().length > 0, {
          message: "Use a nonempty exact observation ID.",
        }),
    )
    .max(max)
    .refine((ids) => new Set(ids).size === ids.length, {
      message: "Each observation ID must appear only once.",
    });
const budget = (min: number, max: number) =>
  z.number().int().min(min).max(max).optional();

export const researchCaptureSchema = z
  .strictObject({
    mode: z.enum(["off", "summary", "research"]),
    observation_window: z
      .strictObject({
        start: z.iso.date("Choose a valid inclusive observation start date."),
        end: z.iso.date("Choose a valid exclusive observation end date."),
      })
      .nullable()
      .optional(),
    stage_ids: scope(100).optional(),
    instrument_ids: scope(10000).optional(),
    max_decisions: budget(1, 1000000),
    max_detail_rows: budget(1, 20000000),
    max_uncompressed_bytes: budget(1024, 1073741824),
  })
  .superRefine((value, context) => {
    if (
      value.observation_window &&
      value.observation_window.start >= value.observation_window.end
    )
      context.addIssue({
        code: "custom",
        path: ["observation_window", "end"],
        message:
          "Observation window must be nonempty and half-open [start, end).",
      });
  });

// A half-entered date is recoverable draft state, never an admitted request.
const draftDate = z.string().regex(/^(\d{4}-\d{2}-\d{2})?$/);
export const researchCaptureDraftSchema = z.strictObject({
  ...researchCaptureSchema.shape,
  observation_window: z
    .strictObject({ start: draftDate, end: draftDate })
    .nullable()
    .optional(),
});

export type ResearchCapture = z.infer<typeof researchCaptureSchema>;

export function parseCaptureScope(text: string): string[] {
  return text
    .split(/[,\r\n]/)
    .map((id) => id.trim())
    .filter(Boolean);
}

/** Capture scope is independent from immutable inputs and execution windows. */
export function withResearchCapture<
  T extends { research_capture?: ResearchCapture | null },
>(body: T, capture: ResearchCapture | undefined): T {
  const next = { ...body };
  if (capture === undefined) delete next.research_capture;
  else next.research_capture = capture;
  return next;
}

export function captureSummary(capture: ResearchCapture | null | undefined) {
  if (capture === null) return "Default summary · stored null retained";
  if (!capture) return "Default summary · no capture override sent";
  if (capture.mode === "off")
    return "Off · research observations not collected";
  return `${capture.mode} · ${
    capture.observation_window
      ? `[${capture.observation_window.start}, ${capture.observation_window.end})`
      : "all execution dates"
  } · ${capture.stage_ids?.length || "all"} stages · ${
    capture.instrument_ids?.length || "all"
  } instruments`;
}
