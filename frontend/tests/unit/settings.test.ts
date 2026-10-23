import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { applyWorkspaceDefaults } from "../../src/lib/settings-api";
import {
  capabilityFixture,
  operatorCapabilityFixture,
  researchFixture,
} from "./research-fixtures";

const saved = {
  initial_cash: 250000,
  max_iterations: 3,
  max_model_calls: 20,
  max_tokens: 50000,
  max_runtime_seconds: 600,
  model_profile_id: "fixture",
};
describe("workspace defaults and management contracts", () => {
  it("creates a new configuration without mutating input or historical hash bytes", () => {
    const { body } = researchFixture();
    const before = JSON.stringify(body);
    const after = applyWorkspaceDefaults(body, saved, capabilityFixture());
    expect(JSON.stringify(body)).toBe(before);
    expect(after).not.toBe(body);
    expect(after.market_profile.initial_cash).toBe(250000);
    expect(after.optimizer.max_iterations).toBe(3);
    expect(after.budgets.max_model_calls).toBe(20);
    expect(after.dataset).toEqual(body.dataset);
    expect(after.model).toEqual(body.model);
  });
  it("does not silently switch an unavailable saved model", () => {
    expect(() =>
      applyWorkspaceDefaults(
        researchFixture().body,
        { ...saved, model_profile_id: "revoked" },
        capabilityFixture(),
      ),
    ).toThrow(/no longer available/);
  });
  it("copies the current admitted operator selection without secret data or old hashes", () => {
    const { capabilities, profile } = operatorCapabilityFixture();
    const result = applyWorkspaceDefaults(
      researchFixture().body,
      { ...saved, model_profile_id: profile.id },
      capabilities,
    );
    expect(result.model).toEqual(profile.config);
    expect(JSON.stringify(result.model)).not.toContain("secret_env");
    expect(JSON.stringify(result.model)).not.toContain("base_url");
  });
  it("publishes exact non-secret management contracts", () => {
    const api = JSON.parse(
      readFileSync(
        new URL("../../../packages/contracts/openapi.json", import.meta.url),
        "utf8",
      ),
    );
    expect(api.components.schemas.SelfProfileUpdate.additionalProperties).toBe(
      false,
    );
    expect(
      Object.keys(api.components.schemas.SelfProfileUpdate.properties),
    ).toEqual(["full_name"]);
    expect(
      Object.keys(api.components.schemas.SecretReferenceUpdate.properties),
    ).toEqual(["enabled"]);
    expect(
      Object.keys(api.components.schemas.SecretReferencePublic.properties),
    ).not.toContain("secret_env");
    expect(
      Object.keys(api.components.schemas.SecretReferencePublic.properties),
    ).not.toContain("base_url");
    expect(
      api.paths["/api/v1/settings/users/{user_id}/quota"].post.operationId,
    ).toBe("update_managed_quota");
  });
});
