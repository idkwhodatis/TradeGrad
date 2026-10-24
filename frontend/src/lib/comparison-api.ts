import { queryOptions } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import type {
  ComparisonCreate,
  ComparisonSectionPage,
  ComparisonResult,
} from "@/client/types.gen";
import { comparisonTerminal } from "./comparison-evidence";
import { type ComparisonSection } from "./workbench-selection";
export const comparisonStatusOptions = (id?: string) =>
  queryOptions({
    queryKey: ["mechanism-comparison", id],
    queryFn: async ({ signal }) =>
      (await sdk.readComparison({ path: { comparison_id: id! }, signal })).data,
    enabled: Boolean(id),
    refetchInterval: (query) =>
      query.state.data && comparisonTerminal.has(query.state.data.status)
        ? false
        : 2000,
  });
export const comparisonResultOptions = (id?: string, hash?: string) =>
  queryOptions({
    queryKey: ["mechanism-comparison-result", id, hash],
    queryFn: async ({ signal }) =>
      (
        await sdk.readComparisonResult({
          path: { comparison_id: id! },
          query: { manifest_sha256: hash },
          signal,
        })
      ).data,
    enabled: Boolean(id && hash),
    staleTime: Infinity,
  });
export const comparisonSectionOptions = (
  id: string,
  hash: string,
  section: ComparisonSection,
  options: {
    event_pair_id?: string;
    instrument_id?: string;
    offset?: number;
  } = {},
) =>
  queryOptions({
    queryKey: [
      "mechanism-comparison-section",
      id,
      hash,
      section,
      options.event_pair_id,
      options.instrument_id,
      options.offset ?? 0,
    ],
    queryFn: async ({ signal }) =>
      (
        await sdk.readComparisonSection({
          path: { comparison_id: id, section },
          query: {
            manifest_sha256: hash,
            ...options,
            offset: options.offset ?? 0,
            limit: 25,
          },
          signal,
        })
      ).data,
    enabled: Boolean(id && hash),
    staleTime: Infinity,
  });
export async function createComparison(body: ComparisonCreate, key: string) {
  return (
    await sdk.createComparison({ body, headers: { "Idempotency-Key": key } })
  ).data;
}
export async function changeComparison(
  id: string,
  action: "cancel" | "retry",
  key: string,
) {
  const options = {
    path: { comparison_id: id },
    headers: { "Idempotency-Key": key },
  };
  return (
    action === "cancel"
      ? await sdk.cancelComparison(options)
      : await sdk.retryComparison(options)
  ).data;
}
export const comparisonStrategyOptions = (offset = 0) =>
  queryOptions({
    queryKey: ["comparison-source-strategies", offset],
    queryFn: async ({ signal }) =>
      (await sdk.listStrategies({ query: { limit: 25, offset }, signal })).data,
  });
/** Check page identity AND the selected range before showing even cached rows. */
export function comparisonPageMatches(
  page: ComparisonSectionPage,
  result: ComparisonResult,
  section: ComparisonSection,
  offset: number,
  pair?: string,
  instrument?: string,
) {
  return (
    page.comparison_id === result.comparison_id &&
    page.input_sha256 === result.input_sha256 &&
    page.manifest_sha256 === result.manifest_sha256 &&
    page.section === section &&
    page.offset === offset &&
    page.limit === 25 &&
    page.items.length <= page.limit &&
    page.items.every(
      (row) =>
        row.section === section &&
        (!pair ||
          row.event_pair_id === pair ||
          (["orders", "fills"].includes(section) &&
            row.associations?.some(
              (association) => association.event_pair_id === pair,
            ))) &&
        (!instrument || row.instrument_id === instrument),
    )
  );
}
