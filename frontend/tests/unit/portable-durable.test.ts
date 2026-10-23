import { beforeEach, afterEach, describe, it, expect, vi } from "vitest";
import {
  clearAcceptedPortableDraft,
  loadPortableDraft,
  restoreMatchingPortableDraft,
  portableCompletionIsCurrent,
  portablePreflightMatches,
  portableSubmissionFor,
  savePortableDraft,
  type PortableDraft,
} from "../../src/features/portable/durable";
import {
  portableFingerprint,
  portableFormSchema,
  portableIssues,
  portableUniverses,
  portableControls,
  portableRequest,
} from "../../src/features/portable/protocol";
import { portableFixture, portableRunFixture } from "./portable-fixtures";
import savedConfig from "../fixtures/portable-config.json";
beforeEach(() => {
  const data = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => data.set(k, v),
    removeItem: (k: string) => data.delete(k),
  });
});
afterEach(() => vi.unstubAllGlobals());
const draft = (): PortableDraft => ({
  version: 1,
  body: portableFixture().body,
  pending: null,
});
describe("distinct portable input contract", () => {
  it("roundtrips the Python-validated v3 fixture without dropping or replacing reviewed semantics", () => {
    const parsed = portableFormSchema.parse(
      JSON.parse(JSON.stringify(savedConfig)),
    );
    expect(portableRequest(parsed)).toEqual(savedConfig);
    expect(parsed.engine_profile).toMatchObject({
      profile_version: "akquant.portable.cash-daily.controller-t1.v3",
      t_plus_one: false,
      engine_settlement_mode: "T+0",
      application_t_plus_one: true,
      application_settlement_enforcement:
        "trusted_controller_daily_next_open_inventory.v1",
      corporate_actions: "reject_window_with_actions",
      sellability_observation: "next_open_unreserved_holdings",
    });
    const value = { version: 1 as const, body: parsed, pending: null };
    expect(savePortableDraft("owner-a", value)).toBe(true);
    expect(loadPortableDraft("owner-a")).toEqual(value);
  });
  it("rejects obsolete, missing, extra or altered pinned profile fields instead of silently upgrading them", () => {
    for (const patch of [
      { profile_version: "akquant.portable.cash-daily.v1" },
      { profile_version: "akquant.portable.cash-daily.controller-t1.v2" },
      { t_plus_one: true },
      { engine_settlement_mode: "T+1" },
      { application_t_plus_one: false },
      { application_settlement_enforcement: "native" },
      { initial_positions: "from_account" },
      { policy_callback: "intraday" },
      { corporate_actions: "ignore_actions" },
      { sellability_observation: "decision_close_holdings" },
      { unsettled_next_open_sell: "allow" },
      { limit_execution: "allow_non_open_fills" },
      { lot_size: 1 },
      { timezone: "America/New_York" },
      { stamp_tax_rate: 0 },
      { unreviewed_option: true },
    ]) {
      expect(
        portableFormSchema.safeParse({
          ...savedConfig,
          engine_profile: { ...savedConfig.engine_profile, ...patch },
        }).success,
      ).toBe(false);
    }
    const incomplete: Record<string, unknown> = {
      ...savedConfig.engine_profile,
    };
    delete incomplete.application_settlement_enforcement;
    expect(
      portableFormSchema.safeParse({
        ...savedConfig,
        engine_profile: incomplete,
      }).success,
    ).toBe(false);
    const obsolete = {
      ...savedConfig,
      engine_profile: {
        ...savedConfig.engine_profile,
        profile_version: "akquant.portable.cash-daily.v1",
      },
    };
    sessionStorage.setItem(
      "tradegrad.portable-draft.v1.owner-a",
      JSON.stringify({ version: 1, body: obsolete, pending: null }),
    );
    expect(loadPortableDraft("owner-a")).toBeNull();
  });
  it("accepts the market-specific US profile without changing its shared controller contract", () => {
    const us = {
      ...savedConfig,
      task: "us_cross",
      engine_profile: {
        ...savedConfig.engine_profile,
        market: "US",
        application_t_plus_one: false,
        stamp_tax_rate: 0,
        lot_size: 1,
        timezone: "America/New_York",
      },
    };
    const parsed = portableFormSchema.parse(us);
    expect(portableRequest(parsed)).toEqual(us);
    expect(portableIssues(parsed)).toEqual([]);
    parsed.initial_cash += 1;
    expect(portableIssues(parsed)).toContain(
      "Pinned engine profile and initial cash differ.",
    );
  });
  it("accepts full Python-generated defaults and rejects native, credentials and extra fields", () => {
    const { body } = portableFixture();
    expect(portableIssues(body)).toEqual([]);
    for (const change of [
      { schema_version: "tradegrad.run.v1" },
      { evaluation_mode: "native_akquant" },
      {
        model: {
          ...body.model,
          provider: "openai",
          credential_ref: "not-allowed",
        },
      },
      { model: { ...body.model, credential_ref: "not-allowed" } },
      { authorization: "not-allowed" },
      { conformance: "certified" },
      { external_framework_equivalence: "certified" },
    ])
      expect(portableFormSchema.safeParse({ ...body, ...change }).success).toBe(
        false,
      );
  });
  it("requires real valid half-open dates and distinct optimization validation", () => {
    const { body } = portableFixture();
    body.windows.train.end = "2024-02-30";
    expect(portableIssues(body).join(" ")).toContain("valid [start, end)");
    body.windows.train.end = "2024-03-02";
    expect(portableIssues(body).join(" ")).toContain("non-overlapping");
    body.windows.validation = null;
    expect(portableIssues(body).join(" ")).toContain("distinct validation");
  });
  it("never treats seed evaluation as optimization or silently changes policy task", () => {
    const { body } = portableFixture();
    body.execution_mode = "seed_evaluation";
    body.optimizer.max_iterations = 1;
    body.strategy_kind = "event";
    body.engine_profile.market = "US";
    expect(portableIssues(body).join(" ")).toContain("zero optimizer");
    expect(portableIssues(body).join(" ")).toContain("Policy kind");
    expect(portableIssues(body).join(" ")).toContain("markets differ");
  });
  it("matches exact universe hash and complete listing membership", () => {
    const { catalog } = portableFixture();
    expect(
      portableUniverses(catalog.datasets[0], catalog.universes),
    ).toHaveLength(1);
    catalog.universes[0].content_sha256 = "0".repeat(64);
    expect(portableUniverses(catalog.datasets[0], catalog.universes)).toEqual(
      [],
    );
    expect(portableUniverses(undefined, catalog.universes)).toEqual([]);
  });
  it("requires a complete checkpoint and confirmed stop before resume", () => {
    const run = portableRunFixture();
    expect(portableControls(run).pause).toBe(true);
    run.status = "cancel_requested";
    expect(portableControls(run)).toMatchObject({
      pause: false,
      cancel: false,
      resume: false,
    });
    run.status = "paused";
    expect(portableControls(run).resumeReason).toContain(
      "No complete checkpoint",
    );
    run.latest_checkpoint_id = "checkpoint";
    run.attempts = [
      {
        id: "attempt",
        status: "paused",
        generation: 1,
        started_at: "now",
        heartbeat_at: "now",
        finished_at: null,
        exit_code: null,
        error: null,
        sandbox_stopped: false,
      },
    ];
    expect(portableControls(run).resume).toBe(false);
    expect(portableControls(run).resumeReason).toContain(
      "both policy and engine",
    );
    run.attempts[0].sandbox_stopped = true;
    expect(portableControls(run).resume).toBe(true);
  });
});
describe("portable owner-scoped durable recovery and race fencing", () => {
  it("restores immutable inputs and key across reload without sharing another owner", () => {
    const value = draft();
    value.pending = portableSubmissionFor(value.body, null);
    expect(savePortableDraft("owner-a", value)).toBe(true);
    expect(loadPortableDraft("owner-a")).toEqual(value);
    expect(loadPortableDraft("owner-b")).toBeNull();
    expect(
      portableSubmissionFor(
        loadPortableDraft("owner-a")!.body,
        loadPortableDraft("owner-a")!.pending,
      ).key,
    ).toBe(value.pending.key);
  });
  it("keeps exact retry key for reordered JSON and changes it for every material edit", () => {
    const { body } = portableFixture();
    const request = portableSubmissionFor(body, null);
    expect(
      portableSubmissionFor(
        Object.fromEntries(Object.entries(body).reverse()) as typeof body,
        request,
      ),
    ).toBe(request);
    for (const edit of [
      (b: typeof body) => b.optimizer.seed++,
      (b: typeof body) => (b.seed_manifest.sha256 = "0".repeat(64)),
      (b: typeof body) => (b.seed_parameters.sha256 = "0".repeat(64)),
      (b: typeof body) => (b.environment_digest = "0".repeat(64)),
      (b: typeof body) => (b.windows.train.start = "2024-01-09"),
      (b: typeof body) => b.budgets.max_tokens--,
    ]) {
      const changed = structuredClone(body);
      edit(changed);
      expect(portableSubmissionFor(changed, request).key).not.toBe(request.key);
    }
    body.optimizer.seed++;
    expect(request.body.optimizer.seed).toBe(0);
  });
  it("persists incomplete dates but does not restore a preflight approval", () => {
    const value = draft();
    value.body.windows.validation!.start = "";
    expect(savePortableDraft("owner-a", value)).toBe(true);
    expect(loadPortableDraft("owner-a")?.body.windows.validation?.start).toBe(
      "",
    );
    expect(loadPortableDraft("owner-a")).not.toHaveProperty("preflight");
  });
  it("rejects corrupt and credential-bearing browser persistence", () => {
    sessionStorage.setItem("tradegrad.portable-draft.v1.owner-a", "{broken");
    expect(loadPortableDraft("owner-a")).toBeNull();
    const value = draft();
    expect(
      savePortableDraft("owner-a", {
        ...value,
        authorization: "forbidden",
      } as PortableDraft),
    ).toBe(false);
    expect(
      savePortableDraft("owner-a", {
        ...value,
        body: {
          ...value.body,
          model: { ...value.body.model, credential_ref: "forbidden" },
        },
      } as unknown as PortableDraft),
    ).toBe(false);
  });
  it("reports storage denial without claiming a saved recovery key", () => {
    vi.stubGlobal("sessionStorage", {
      getItem() {
        throw Error("denied");
      },
      setItem() {
        throw Error("quota");
      },
      removeItem() {
        throw Error("denied");
      },
    });
    expect(loadPortableDraft("owner-a")).toBeNull();
    expect(savePortableDraft("owner-a", draft())).toBe(false);
    expect(
      clearAcceptedPortableDraft(
        "owner-a",
        portableSubmissionFor(draft().body, null),
      ),
    ).toBe(false);
  });
  it("preflight is valid only for the current exact body", () => {
    const { body } = portableFixture();
    const checked = {
      fingerprint: portableFingerprint(body),
      result: {
        valid: true as const,
        config_sha256: "a".repeat(64),
        evaluation_windows: [],
        is_synthetic: true,
        warnings: [],
        holdout_execution: false as const,
        runtime_certified: false as const,
      },
    };
    expect(portablePreflightMatches(body, checked)).toBe(true);
    body.warmup.sessions++;
    expect(portablePreflightMatches(body, checked)).toBe(false);
    expect(portablePreflightMatches(body, null)).toBe(false);
  });
  it("ignores completions after unmount, owner switch, new key or input edits", () => {
    const value = draft();
    const request = portableSubmissionFor(value.body, null);
    const current = {
      active: true,
      owner: "a",
      requestOwner: "a",
      body: value.body,
      pending: request,
    };
    expect(portableCompletionIsCurrent(request, current)).toBe(true);
    expect(
      portableCompletionIsCurrent(request, { ...current, active: false }),
    ).toBe(false);
    expect(
      portableCompletionIsCurrent(request, { ...current, owner: "b" }),
    ).toBe(false);
    expect(
      portableCompletionIsCurrent(request, {
        ...current,
        pending: portableSubmissionFor(value.body, null),
      }),
    ).toBe(false);
    const changed = structuredClone(value.body);
    changed.optimizer.seed++;
    expect(
      portableCompletionIsCurrent(request, { ...current, body: changed }),
    ).toBe(false);
  });
  it("old responses never erase newer or edited persisted requests", () => {
    const value = draft();
    const a = portableSubmissionFor(value.body, null);
    value.pending = a;
    savePortableDraft("owner-a", value);
    value.body.optimizer.seed++;
    savePortableDraft("owner-a", value);
    expect(clearAcceptedPortableDraft("owner-a", a)).toBe(false);
    value.pending = portableSubmissionFor(value.body, a);
    savePortableDraft("owner-a", value);
    expect(clearAcceptedPortableDraft("owner-a", a)).toBe(false);
    expect(clearAcceptedPortableDraft("owner-b", value.pending)).toBe(false);
    expect(loadPortableDraft("owner-a")).toEqual(value);
    expect(clearAcceptedPortableDraft("owner-a", value.pending)).toBe(true);
  });
  it.each(["A-first", "B-first"])(
    "preserves reopened drafts with %s completion ordering",
    async (order) => {
      const value = draft();
      const a = portableSubmissionFor(value.body, null);
      value.pending = a;
      savePortableDraft("owner-a", value);
      const reopened = loadPortableDraft("owner-a")!;
      reopened.body.optimizer.seed++;
      const b = portableSubmissionFor(reopened.body, a);
      reopened.pending = b;
      savePortableDraft("owner-a", reopened);
      let finishA!: () => void, finishB!: () => void;
      const responseA = new Promise<void>((resolve) => {
        finishA = resolve;
      }).then(() => clearAcceptedPortableDraft("owner-a", a));
      const responseB = new Promise<void>((resolve) => {
        finishB = resolve;
      }).then(() => clearAcceptedPortableDraft("owner-a", b));
      if (order === "A-first") {
        finishA();
        expect(await responseA).toBe(false);
        expect(loadPortableDraft("owner-a")).toEqual(reopened);
        finishB();
        expect(await responseB).toBe(true);
      } else {
        finishB();
        expect(await responseB).toBe(true);
        const fresh = draft();
        savePortableDraft("owner-a", fresh);
        finishA();
        expect(await responseA).toBe(false);
        expect(loadPortableDraft("owner-a")).toEqual(fresh);
      }
    },
  );
});

it("does not restore a superseded configuration or retry key after a blocked storage write", () => {
  const { body } = portableFixture();
  const pending = portableSubmissionFor(body, null);
  expect(savePortableDraft("owner-a", { version: 1, body, pending })).toBe(
    true,
  );
  const next = structuredClone(body);
  next.warmup.sessions += 1;
  vi.spyOn(sessionStorage, "setItem").mockImplementation(() => {
    throw Error("quota");
  });
  expect(
    savePortableDraft("owner-a", { version: 1, body: next, pending: null }),
  ).toBe(false);
  expect(restoreMatchingPortableDraft("owner-a", next)).toBeNull();
  expect(restoreMatchingPortableDraft("owner-a", body)?.pending?.key).toBe(
    pending.key,
  );
  expect(restoreMatchingPortableDraft("owner-b", body)).toBeNull();
});
