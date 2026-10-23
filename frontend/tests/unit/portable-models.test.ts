import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { portableFixture } from "./portable-fixtures";
import { operatorCapabilityFixture } from "./research-fixtures";
import {
  researchModelProfiles,
  researchModelProfileFor,
} from "../../src/lib/research-form";
import {
  portableFormSchema,
  portableFingerprint,
} from "../../src/features/portable/protocol";
import {
  loadPortableDraft,
  savePortableDraft,
  portableSubmissionFor,
} from "../../src/features/portable/durable";

beforeEach(() => {
  const items = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => items.get(key) ?? null,
    setItem: (key: string, value: string) => items.set(key, value),
    removeItem: (key: string) => items.delete(key),
  });
});
afterEach(() => vi.unstubAllGlobals());

function portableProfile() {
  const { capabilities, profile } = operatorCapabilityFixture();
  profile.strategy_modes = ["native_akquant", "portable"];
  return { capabilities, profile };
}

describe("portable operator profile admission without model execution", () => {
  it("preserves fixture default and requires mode-specific complete owner choices", () => {
    const { capabilities, profile } = portableProfile();
    expect(portableFixture().body.model.provider).toBe("fixture");
    expect(
      researchModelProfiles(capabilities, "portable").map((p) => p.id),
    ).toEqual(["fixture", profile.id]);
    expect(
      researchModelProfileFor(profile.config, capabilities, "portable"),
    ).toEqual(profile);
    profile.strategy_modes = ["native_akquant"];
    expect(
      researchModelProfileFor(profile.config, capabilities, "portable"),
    ).toBeUndefined();
  });

  it.each(["profile_sha256", "model", "profile_id"] as const)(
    "rejects stale %s without rewriting a saved request",
    (field) => {
      const { capabilities, profile } = portableProfile();
      const model = {
        ...profile.config,
        [field]: field === "profile_sha256" ? "a".repeat(64) : "changed",
      };
      expect(
        researchModelProfileFor(model, capabilities, "portable"),
      ).toBeUndefined();
    },
  );

  it("preserves complete profile hash and pending idempotency identity across reload", () => {
    const { profile } = portableProfile();
    const { body } = portableFixture();
    const selected = portableFormSchema.parse({
      ...body,
      model: profile.config,
    });
    expect(portableFingerprint(selected)).not.toEqual(
      portableFingerprint(body),
    );
    const pending = portableSubmissionFor(selected, null);
    expect(
      savePortableDraft("owner-a", { version: 1, body: selected, pending }),
    ).toBe(true);
    const restored = loadPortableDraft("owner-a")!;
    expect(restored.body.model).toEqual(profile.config);
    expect(portableSubmissionFor(restored.body, restored.pending)).toEqual(
      pending,
    );
    expect(loadPortableDraft("owner-b")).toBeNull();
  });

  it.each(["base_url", "secret_env", "api_key"])(
    "rejects browser field %s",
    (field) => {
      const { profile } = portableProfile();
      expect(
        portableFormSchema.safeParse({
          ...portableFixture().body,
          model: { ...profile.config, [field]: "not-a-credential" },
        }).success,
      ).toBe(false);
    },
  );
});
