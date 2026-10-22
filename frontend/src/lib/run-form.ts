import { z } from "zod";

export const runFormSchema = z
  .object({
    task: z.enum(["cn_time", "cn_cross", "us_time", "us_cross"]),
    dataset_version_id: z.uuid("Choose a dataset snapshot."),
    strategy_version_id: z.uuid("Choose a strategy version."),
    start_date: z.iso.date("Choose a valid start date."),
    end_date: z.iso.date("Choose a valid end date."),
    profile: z.literal("corrected_explicit_config").optional(),
  })
  .refine((value) => value.start_date < value.end_date, {
    path: ["end_date"],
    message: "End date must be later than the start date (exclusive).",
  });
