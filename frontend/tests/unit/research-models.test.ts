import { describe, expect, it } from "vitest";
import {
  researchDefaults,
  researchFormSchema,
  researchModelProfileFor,
  researchModelProfiles,
  researchModelSchema,
  researchSelectionIssues,
} from "../../src/lib/research-form";
import { portableFormSchema } from "../../src/features/portable/protocol";
import { portableFixture } from "./portable-fixtures";
import {
  capabilityFixture,
  operatorCapabilityFixture,
  researchFixture,
} from "./research-fixtures";

describe("owner-scoped operator model selections", () => {
  it("keeps fixture as the default even when paid profiles are available", () => {
    const { capabilities } = operatorCapabilityFixture();
    const { catalog } = researchFixture();
    expect(researchDefaults(capabilities, catalog).model).toEqual(
      capabilityFixture().defaults!.model,
    );
    expect(researchModelProfiles(capabilities).map((row) => row.id)).toEqual([
      "fixture",
      "research-profile",
    ]);
  });

  it.each(["openai", "openrouter", "openai-compatible"] as const)(
    "accepts only the complete admitted %s profile",
    (provider) => {
      const { capabilities, profile } = operatorCapabilityFixture();
      profile.config.provider = provider;
      capabilities.model_providers = ["fixture", provider];
      const { body, catalog } = researchFixture();
      body.model = structuredClone(profile.config);
      expect(researchFormSchema.safeParse(body).success).toBe(true);
      expect(researchSelectionIssues(body, catalog, capabilities)).toEqual([]);
      expect(researchModelProfileFor(body.model, capabilities)).toEqual(
        profile,
      );
      expect(
        researchModelProfileFor(body.model, capabilityFixture()),
      ).toBeUndefined();
    },
  );

  it.each([
    ["owner_identity", { profile_id: "other-owner-profile" }],
    ["profile_hash", { profile_sha256: "c".repeat(64) }],
    ["model_identity", { model: "unlisted-model" }],
    ["provider", { provider: "openai" as const }],
    ["temperature", { temperature: 0.5 }],
    ["output_limit", { max_output_tokens: 2049 }],
  ] as const)("rejects a stale or altered profile field: %s", (_, changed) => {
    const { capabilities, profile } = operatorCapabilityFixture();
    const { body, catalog } = researchFixture();
    body.model = { ...profile.config, ...changed };
    expect(researchSelectionIssues(body, catalog, capabilities)).toContainEqual(
      {
        path: "model.profile_id",
        message: expect.stringContaining(
          "unavailable or changed for this owner",
        ),
      },
    );
  });

  it("fails closed on unavailable, duplicate or inconsistent capability choices", () => {
    const { capabilities, profile } = operatorCapabilityFixture();
    for (const changed of [
      { model_profiles: [] },
      { model_profiles: [profile, profile] },
      { model_profiles: [{ ...profile, id: "different-id" }] },
      { model_profiles: [{ ...profile, strategy_modes: [] }] },
      {
        model_profiles: [
          { ...profile, strategy_modes: ["portable"] as Array<"portable"> },
        ],
      },
      { model_providers: ["fixture"] },
      { models: ["deterministic-fixture-v1"] },
      { live_model_calls: false },
    ]) {
      expect(
        researchModelProfileFor(profile.config, {
          ...capabilities,
          ...changed,
        }),
      ).toBeUndefined();
    }
  });

  it("honors synthetic operator profiles without claiming live model access", () => {
    const { capabilities, profile } = operatorCapabilityFixture(true);
    expect(capabilities.live_model_calls).toBe(false);
    expect(
      researchModelProfileFor(profile.config, capabilities)?.synthetic,
    ).toBe(true);
  });

  it("rejects a paid default or missing fixture rather than auto-selecting paid access", () => {
    const { capabilities, profile } = operatorCapabilityFixture();
    const { catalog } = researchFixture();
    expect(() =>
      researchDefaults(
        {
          ...capabilities,
          defaults: { ...capabilities.defaults, model: profile.config },
        },
        catalog,
      ),
    ).toThrow("deterministic fixture default");
    expect(() =>
      researchDefaults({ ...capabilities, model_profiles: [profile] }, catalog),
    ).toThrow("deterministic fixture default");
  });
});

describe("secret-free pinned model form", () => {
  const cases: Array<{
    name: string;
    change: (credential: string) => Record<string, unknown>;
  }> = [
    {
      name: "credential_reference",
      change: (credential) => ({ credential_ref: credential }),
    },
    {
      name: "endpoint_field",
      change: () => ({ endpoint: "https://example.invalid/v1" }),
    },
    {
      name: "credential_field",
      change: (credential) => ({ api_key: credential }),
    },
    {
      name: "model_url",
      change: () => ({ model: "https://example.invalid/v1" }),
    },
    {
      name: "credential_shaped_model",
      change: (credential) => ({ model: `sk-${credential}` }),
    },
    {
      name: "profile_url",
      change: () => ({ profile_id: "https://example.invalid/v1" }),
    },
    { name: "missing_profile", change: () => ({ profile_id: null }) },
    { name: "missing_hash", change: () => ({ profile_sha256: null }) },
    { name: "invalid_hash", change: () => ({ profile_sha256: "not-a-hash" }) },
    { name: "negative_temperature", change: () => ({ temperature: -0.1 }) },
    { name: "excessive_temperature", change: () => ({ temperature: 2.1 }) },
    { name: "zero_output_limit", change: () => ({ max_output_tokens: 0 }) },
    {
      name: "excessive_output_limit",
      change: () => ({ max_output_tokens: 16385 }),
    },
    {
      name: "fractional_output_limit",
      change: () => ({ max_output_tokens: 2.5 }),
    },
  ];
  // Report only stable case names and boolean outcomes, never credential values.
  it.each(cases)(
    "rejects unbounded, secret-bearing or incomplete model config: $name",
    ({ change }) => {
      const { profile } = operatorCapabilityFixture();
      const changed = change(crypto.randomUUID());
      expect(
        researchModelSchema.safeParse({ ...profile.config, ...changed })
          .success,
      ).toBe(false);
    },
  );

  it("does not accept a fixture carrying an operator identity", () => {
    const { body } = researchFixture();
    expect(
      researchModelSchema.safeParse({
        ...body.model,
        profile_id: "research-profile",
        profile_sha256: "b".repeat(64),
      }).success,
    ).toBe(false);
  });

  it("accepts a pinned portable model shape but still requires mode-specific admission", () => {
    const { body } = portableFixture();
    const { profile } = operatorCapabilityFixture();
    expect(portableFormSchema.safeParse(body).success).toBe(true);
    expect(
      portableFormSchema.safeParse({ ...body, model: profile.config }).success,
    ).toBe(true);
    expect(profile.strategy_modes).toEqual(["native_akquant"]);
    expect(capabilityFixture().model_profiles![0].strategy_modes).toEqual([
      "native_akquant",
      "portable",
    ]);
  });
});
