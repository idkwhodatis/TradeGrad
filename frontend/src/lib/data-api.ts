import { queryOptions } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import { importTerminal } from "./data-utils";

export const providersOptions = queryOptions({
  queryKey: ["data-providers"],
  queryFn: async ({ signal }) => (await sdk.listDataProviders({ signal })).data,
});
export const listingsOptions = (
  offset = 0,
  listingIds?: string[],
  filters: import("@/components/named-list-controls").NamedListFilters = {},
) =>
  queryOptions({
    queryKey: [
      "data-listings",
      offset,
      listingIds,
      ...(Object.keys(filters).length ? [filters] : []),
    ],
    queryFn: async ({ signal }) =>
      (
        await sdk.listDataListings({
          query: { offset, limit: 100, listing_ids: listingIds, ...filters },
          signal,
        })
      ).data,
  });
export const importsOptions = (offset = 0) =>
  queryOptions({
    queryKey: ["data-imports", offset],
    queryFn: async ({ signal }) =>
      (await sdk.listDataImports({ query: { offset, limit: 25 }, signal }))
        .data,
    refetchInterval: 3000,
  });
export const importOptions = (id: string, blockOffset = 0, blockLimit = 10) =>
  queryOptions({
    queryKey: ["data-import", id, blockOffset, blockLimit],
    queryFn: async ({ signal }) =>
      (
        await sdk.readDataImport({
          path: { import_id: id },
          query: { block_offset: blockOffset, block_limit: blockLimit },
          signal,
        })
      ).data,
    refetchInterval: (query) =>
      query.state.data && importTerminal.has(query.state.data.status)
        ? false
        : 1500,
  });
export const universesOptions = (offset = 0) =>
  queryOptions({
    queryKey: ["data-universes", offset],
    queryFn: async ({ signal }) =>
      (await sdk.listDataUniverses({ query: { offset, limit: 50 }, signal }))
        .data,
  });
export const datasetOptions = (id: string) =>
  queryOptions({
    queryKey: ["dataset", id],
    queryFn: async ({ signal }) =>
      (await sdk.readDataset({ path: { dataset_id: id }, signal })).data,
  });
export const coverageOptions = (id: string) =>
  queryOptions({
    queryKey: ["dataset-coverage", id],
    queryFn: async ({ signal }) =>
      (await sdk.readDataCoverage({ path: { dataset_id: id }, signal })).data,
  });
export const previewOptions = (
  id: string,
  listingId: string,
  start: string,
  end: string,
  offset: number,
) =>
  queryOptions({
    queryKey: ["dataset-preview", id, listingId, start, end, offset],
    queryFn: async ({ signal }) =>
      (
        await sdk.previewDataSnapshot({
          path: { dataset_id: id },
          query: {
            listing_id: listingId,
            start_date: start,
            end_date: end,
            offset,
            limit: 100,
          },
          signal,
        })
      ).data,
    enabled: Boolean(listingId && start && end),
  });
