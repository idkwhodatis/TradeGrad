import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import type { ExplorationContext } from "../../src/client/types.gen";
import { client } from "../../src/client/client.gen";
import {
  explorationMatches,
  modeAnalysisOptions,
  modeReplayOptions,
  type ExplorationMode,
} from "../../src/lib/exploration-api";

const reference = {
  evaluation_id: "evaluation",
  artifact_sha256: "a".repeat(64),
  window: { start: "2024-01-01", end: "2024-02-01" },
};
function context(mode: ExplorationMode): ExplorationContext {
  return {
    mode,
    evaluation_id: reference.evaluation_id,
    run_id: "run",
    artifact_sha256: reference.artifact_sha256,
    dataset_id: "dataset",
    dataset_sha256: "b".repeat(64),
    split: mode.endsWith("final") ? "holdout" : "train",
    start: reference.window.start,
    end: reference.window.end,
    timezone: "Asia/Shanghai",
    listings: [],
    selection_id: mode.endsWith("final") ? "selection" : null,
    result_trust: mode.startsWith("portable")
      ? "isolated_policy_trusted_akquant_engine"
      : "native_strategy_and_engine_share_process",
    ledger_origin: mode.startsWith("portable")
      ? "trusted_engine"
      : "native_same_process",
  };
}
afterEach(() => vi.unstubAllGlobals());
describe("mode and immutable-artifact projection boundaries", () => {
  it.each<ExplorationMode>([
    "native_research",
    "native_final",
    "portable_research",
    "portable_final",
  ])("accepts exact %s context only", (mode) => {
    const expected = {
      ...reference,
      ...(mode.endsWith("final")
        ? { selection_id: "selection", split: "holdout" }
        : {}),
    };
    expect(explorationMatches(context(mode), mode, expected)).toBe(true);
    expect(
      explorationMatches(
        { ...context(mode), artifact_sha256: "c".repeat(64) },
        mode,
        expected,
      ),
    ).toBe(false);
    expect(
      explorationMatches(
        { ...context(mode), evaluation_id: "other" },
        mode,
        expected,
      ),
    ).toBe(false);
  });
  it.each([
    "mode",
    "selection_id",
    "start",
    "end",
    "split",
    "result_trust",
    "ledger_origin",
  ])("rejects changed %s before displaying holdout values", (field) => {
    expect(
      explorationMatches(
        { ...context("portable_final"), [field]: "different" },
        "portable_final",
        { ...reference, selection_id: "selection" },
      ),
    ).toBe(false);
  });
  it("separates mode, artifact and listing cache identities", () => {
    const query = { listing_id: "listing", limit: 250 };
    expect(
      modeReplayOptions("native_research", reference, query).queryKey,
    ).not.toEqual(
      modeReplayOptions("portable_research", reference, query).queryKey,
    );
    expect(
      modeReplayOptions("portable_research", reference, query).queryKey,
    ).not.toEqual(
      modeReplayOptions(
        "portable_research",
        { ...reference, artifact_sha256: "c".repeat(64) },
        query,
      ).queryKey,
    );
    expect(
      modeReplayOptions("portable_research", reference, query).queryKey,
    ).not.toEqual(
      modeReplayOptions("portable_research", reference, {
        ...query,
        listing_id: "other",
      }).queryKey,
    );
  });
  it.each<ExplorationMode>([
    "native_final",
    "portable_research",
    "portable_final",
  ])(
    "uses separate generated %s read endpoint and rejects swapped response",
    async (mode) => {
      const seen: string[] = [];
      let returned = context(mode);
      client.setConfig({
        baseUrl: "https://unit.test",
        fetch: async (request) => {
          seen.push((request as Request).url);
          return new Response(JSON.stringify({ context: returned }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        },
      });
      const expected = {
        ...reference,
        ...(mode.endsWith("final") ? { selection_id: "selection" } : {}),
      };
      const query = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      await query.fetchQuery(modeAnalysisOptions(mode, expected));
      const prefix = {
        native_final: "/final-evaluations/evaluations/",
        portable_research: "/portable-evaluations/",
        portable_final: "/portable-final-evaluations/evaluations/",
        native_research: "/research-evaluations/",
      }[mode];
      expect(seen[0]).toContain(prefix + "evaluation/return-analysis");
      returned = { ...returned, artifact_sha256: "f".repeat(64) };
      query.clear();
      await expect(
        query.fetchQuery(modeAnalysisOptions(mode, expected)),
      ).rejects.toThrow("verified mode");
      query.clear();
    },
  );
});
