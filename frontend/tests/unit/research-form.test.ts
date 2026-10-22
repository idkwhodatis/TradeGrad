import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import {
  researchFormSchema,
  researchDefaults,
  researchSelectionIssues,
  matchingUniverses,
  suggestWindows,
  researchStepFor,
  profileDescriptions,
} from "../../src/lib/research-form";
import { capabilityFixture, researchFixture } from "./research-fixtures";

const capabilities = capabilityFixture();
describe("complete configured research form", () => {
  it.each(["cn_time", "us_time", "cn_cross", "us_cross"] as const)(
    "binds both supported profiles for %s",
    (task) => {
      for (const profile of [
        "source_compat",
        "corrected_explicit_config",
      ] as const) {
        const { body, catalog } = researchFixture(task, profile);
        expect(researchFormSchema.safeParse(body).success).toBe(true);
        expect(researchSelectionIssues(body, catalog, capabilities)).toEqual(
          [],
        );
      }
    },
  );
  it("binds every API RunConfig and nested field without dropping or inventing keys", () => {
    const { body } = researchFixture();
    const api = JSON.parse(
      readFileSync(
        new URL("../../../packages/contracts/openapi.json", import.meta.url),
        "utf8",
      ),
    );
    const pairs = [
      ["ResearchRunCreate", body],
      ["MarketConfig", body.market_profile],
      ["WarmupConfig", body.warmup],
      ["OptimizerConfig", body.optimizer],
      ["ModelConfig", body.model],
      ["ObjectiveConfig", body.objective],
      ["BudgetConfig", body.budgets],
      ["SlippageConfig", body.market_profile.slippage],
      ["WindowSet", body.windows],
      ["VersionRef", body.dataset],
    ] as const;
    for (const [name, value] of pairs)
      expect(Object.keys(value).sort(), name).toEqual(
        Object.keys(api.components.schemas[name].properties).sort(),
      );
  });
  it("defaults to corrected configuration and authoritative deployment digest", () => {
    const { catalog } = researchFixture("us_cross");
    const body = researchDefaults(capabilities, catalog);
    expect(body.profile).toBe("corrected_explicit_config");
    expect(body.task).toBe("us_cross");
    expect(body.environment_digest).toBe(capabilities.environment_digest);
    expect(body.warmup.sessions).toBe(3);
    expect(body.model.credential_ref).toBeNull();
    expect(researchFormSchema.safeParse(body).success).toBe(true);
  });
  it("does not silently substitute another dataset for an unavailable preferred version", () => {
    const { catalog } = researchFixture();
    expect(() =>
      researchDefaults(capabilities, catalog, crypto.randomUUID()),
    ).toThrow("requested published dataset");
  });
  it("fails closed on unsupported or incomplete server defaults", () => {
    const { catalog } = researchFixture();
    expect(() =>
      researchDefaults({ ...capabilities, defaults: undefined }, catalog),
    ).toThrow("incomplete");
    expect(() =>
      researchDefaults({ ...capabilities, profile_defaults: [] }, catalog),
    ).toThrow("no defaults");
  });
  it("explains source cold starts and does not imply broker terms or reproduction", () => {
    expect(profileDescriptions.source_compat).toContain(
      "zero-history cold starts",
    );
    expect(profileDescriptions.source_compat).toContain(
      "not full paper reproduction",
    );
    expect(profileDescriptions.corrected_explicit_config).toContain(
      "not a claim",
    );
  });
});
describe("half-open windows and actual trading semantics", () => {
  it("accepts adjacent boundaries and nullable unexecuted holdout", () => {
    const { body } = researchFixture();
    expect(
      researchFormSchema.safeParse({
        ...body,
        windows: { ...body.windows, holdout: null },
      }).success,
    ).toBe(true);
  });
  it.each([
    ["train", "start", "2024-02-30"],
    ["train", "end", "2024-03-01"],
    ["validation", "start", "2024-07-31"],
    ["holdout", "start", "2024-10-31"],
    ["holdout", "end", "2024-11-01"],
  ] as const)("rejects %s %s = %s", (split, edge, value) => {
    const { body } = researchFixture();
    body.windows[split]![edge] = value;
    expect(researchFormSchema.safeParse(body).success).toBe(false);
  });
  it("requires validation even for zero-iteration configured research", () => {
    const { body } = researchFixture();
    body.optimizer.max_iterations = 0;
    expect(researchFormSchema.safeParse(body).success).toBe(true);
    expect(
      researchFormSchema.safeParse({
        ...body,
        windows: { ...body.windows, validation: null },
      }).success,
    ).toBe(false);
  });
  it.each([0, 19, 253, 20.5])(
    "rejects invalid corrected timing warm-up %s",
    (sessions) => {
      const { body } = researchFixture();
      body.warmup.sessions = sessions;
      expect(researchFormSchema.safeParse(body).success).toBe(false);
    },
  );
  it("keeps volume units distinct from order lots and rates as fractions", () => {
    const { body } = researchFixture();
    body.market_profile.commission_rate = 5;
    expect(researchFormSchema.safeParse(body).success).toBe(false);
    body.market_profile.commission_rate = 0.0005;
    body.market_profile.lot_size = 10;
    expect(researchFormSchema.safeParse(body).success).toBe(false);
  });
  it("rejects US defaults applied to CN or missing explicit US shorting", () => {
    const { body } = researchFixture();
    body.market_profile.currency = "USD";
    expect(researchFormSchema.safeParse(body).success).toBe(false);
    const { body: us } = researchFixture("us_time");
    us.market_profile.risk_config = null;
    expect(researchFormSchema.safeParse(us).success).toBe(false);
  });
  it("reserves suggested history but leaves real available-session verification to server", () => {
    const { catalog } = researchFixture();
    const snapshot = catalog.datasets[0];
    const suggested = suggestWindows(snapshot, 20);
    expect(suggested.train.start > snapshot.start_date).toBe(true);
    expect(suggested.train.end).toBe(suggested.validation.start);
    expect(suggested.validation.end).toBe(snapshot.end_date);
    expect(suggested.holdout).toBeNull();
  });
});
describe("exact resource references and capability checks", () => {
  it("rejects hash drift, missing refs, mismatched seeds and changed deployment", () => {
    const { body, catalog } = researchFixture();
    body.dataset.sha256 = "b".repeat(64);
    body.seed_strategy.id = crypto.randomUUID();
    body.environment_digest = "b".repeat(64);
    expect(
      researchSelectionIssues(body, catalog, capabilities).map(
        (error) => error.path,
      ),
    ).toEqual(
      expect.arrayContaining([
        "dataset.id",
        "seed_strategy.id",
        "environment_digest",
      ]),
    );
  });
  it("requires exact universe listings and pinned content hash", () => {
    const { catalog } = researchFixture();
    expect(
      matchingUniverses(catalog.datasets[0], catalog.universes),
    ).toHaveLength(1);
    catalog.universes[0].members.push({ listing_id: "extra" });
    expect(matchingUniverses(catalog.datasets[0], catalog.universes)).toEqual(
      [],
    );
    catalog.universes[0].members.pop();
    catalog.universes[0].content_sha256 = "c".repeat(64);
    expect(matchingUniverses(catalog.datasets[0], catalog.universes)).toEqual(
      [],
    );
  });
  it("blocks unknown units, coverage breaches and current membership back-dating", () => {
    const { body, catalog } = researchFixture();
    catalog.datasets[0].manifest.quality = {
      unknown_flags: ["volume_unit_unknown"],
    };
    catalog.universes[0].state = "current_snapshot";
    catalog.universes[0].as_of = "2024-05-01T00:00:00Z";
    body.windows.holdout!.end = "2025-02-01";
    const errors = researchSelectionIssues(body, catalog, capabilities);
    expect(errors.map((error) => error.path)).toEqual(
      expect.arrayContaining([
        "dataset.id",
        "windows.train.start",
        "windows.holdout.end",
      ]),
    );
    expect(errors.some((error) => error.message.includes("units"))).toBe(true);
  });
  it("blocks timing baskets and source-compatible engine overrides", () => {
    const { body, catalog } = researchFixture("cn_time", "source_compat");
    catalog.datasets[0].manifest.listings = [
      { listing_id: "listing-a" },
      { listing_id: "listing-b" },
    ];
    body.market_profile.initial_cash += 1;
    expect(
      researchSelectionIssues(body, catalog, capabilities).map(
        (error) => error.path,
      ),
    ).toEqual(expect.arrayContaining(["dataset.id", "profile"]));
  });
  it("does not silently accept unsupported model, objective or secret fields", () => {
    const { body } = researchFixture();
    for (const model of [
      { ...body.model, credential_ref: "browser-secret-not-permitted" },
      { ...body.model, temperature: 0.2 },
      { ...body.model, provider: "openai" },
    ])
      expect(researchFormSchema.safeParse({ ...body, model }).success).toBe(
        false,
      );
    expect(
      researchFormSchema.safeParse({
        ...body,
        objective: { ...body.objective, ar_weight: 0.7 },
      }).success,
    ).toBe(false);
    expect(
      researchFormSchema.safeParse({ ...body, authorization: "unexpected" })
        .success,
    ).toBe(false);
  });
  it.each(["dataset.id", "windows.holdout.start", "budgets.max_tokens"])(
    "locates field errors in a recoverable wizard step: %s",
    (path) => {
      expect(researchStepFor(path)).toBe(
        path.startsWith("dataset") ? 0 : path.startsWith("windows") ? 1 : 2,
      );
    },
  );
});
