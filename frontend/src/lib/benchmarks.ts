import { z } from "zod";
import type { BenchmarkConfig, DatasetPublic } from "@/client/types.gen";
import { asRecord } from "./data-utils";

export const benchmarkSchema = z
  .strictObject({
    definition_version: z.literal("daily_close_observed_anchor.v1"),
    dataset: z.strictObject({
      id: z.string().uuid(),
      sha256: z.string().regex(/^[0-9a-f]{64}$/),
    }),
    listing_id: z.string().uuid(),
    price_field: z.literal("close"),
  })
  .nullable();
export function benchmarkOptions(datasets: DatasetPublic[], market: string) {
  return datasets
    .filter((item) => item.market === market)
    .flatMap((dataset) => {
      const listings = dataset.manifest.listings;
      return (Array.isArray(listings) ? listings : []).flatMap((item) => {
        const listing = asRecord(item);
        if (typeof listing.listing_id !== "string") return [];
        return [
          {
            key: `${dataset.id}:${listing.listing_id}`,
            label: `${dataset.name} · ${listing.symbol || listing.listing_id} · ${dataset.content_sha256.slice(0, 12)}${dataset.is_synthetic ? " · SYNTHETIC" : ""}`,
            value: {
              definition_version: "daily_close_observed_anchor.v1" as const,
              dataset: { id: dataset.id, sha256: dataset.content_sha256 },
              listing_id: listing.listing_id,
              price_field: "close" as const,
            },
          },
        ];
      });
    });
}
export function benchmarkKey(value: BenchmarkConfig | null | undefined) {
  return value ? `${value.dataset.id}:${value.listing_id}` : "";
}
export function benchmarkSelectionIssue(
  value: BenchmarkConfig | null | undefined,
  datasets: DatasetPublic[],
  market: string,
) {
  if (!value) return null;
  const option = benchmarkOptions(datasets, market).find(
    (item) => item.key === benchmarkKey(value),
  );
  return !option || option.value.dataset.sha256 !== value.dataset.sha256
    ? "The exact benchmark dataset/listing/hash is unavailable. Select an owned published snapshot or explicitly choose None."
    : null;
}
