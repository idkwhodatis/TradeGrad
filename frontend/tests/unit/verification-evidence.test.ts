import { describe, expect, it } from "vitest";
import { portableFixture } from "./portable-fixtures";
import type { IndependentEvaluationConfig } from "../../src/client/types.gen";
import {
  controlledStatusProblem,
  diagnosticResultPins,
  fieldConditions,
  independentStatusProblem,
  previewArmChanges,
  sameIndependentSource,
  verificationSource,
} from "../../src/lib/verification-evidence";
import {
  diagnosticEvidenceClass,
  diagnosticParityProblem,
  parityPageMatches,
  parityReplayTarget,
  requiredDiagnosticChecks,
} from "../../src/lib/diagnostic-parity-evidence";
import {
  capability,
  controlledExperiment,
  independentJob,
  independentPin,
  independentSource,
  parityPage,
  parityReport,
  verificationHashes as h,
  verificationIds as id,
  verificationSelection,
} from "./verification-fixtures";
import { hashes, leaf, spec } from "./workbench-fixtures";

describe("exact original source admission before capability or dispatch", () => {
  it("accepts an admitted physical leaf and never infers candidate/seed association", () => {
    expect(verificationSource(verificationSelection, spec, leaf, true)).toEqual(
      independentSource,
    );
    expect(() =>
      verificationSource(verificationSelection, spec, leaf, false),
    ).toThrow("admitted");
    expect(() =>
      verificationSource(
        verificationSelection,
        spec,
        {
          ...leaf,
          source_association: {
            ...leaf.source_association,
            strategy_basis: "unavailable",
          },
        },
        true,
      ),
    ).toThrow("exact source");
  });
  it.each([
    "code_sha256",
    "spec_sha256",
    "evaluation_id",
    "execution_attempt_id",
    "result_manifest_sha256",
    "trace_manifest_sha256",
  ] as const)("rejects stale %s before loading capability", (field) =>
    expect(() =>
      verificationSource(
        { ...verificationSelection, [field]: "newer" },
        spec,
        leaf,
        true,
      ),
    ).toThrow(),
  );
  it("distinguishes historical absent trace from registered off-mode hash", () => {
    const empty = { ...leaf, trace_manifest_sha256: null };
    const source = verificationSource(
      {
        ...verificationSelection,
        trace_manifest_sha256: undefined,
        trace_status: "not_collected",
      },
      spec,
      empty,
      true,
    );
    expect(source.trace_manifest_sha256).toBeNull();
    expect(sameIndependentSource(source, independentSource)).toBe(false);
  });
  it("retains zero/false/null protocol fields and nested immutable conditions", () => {
    expect(
      fieldConditions({
        warmup: { sessions: 0 },
        state: null,
        fee: { enabled: false },
        pool: [],
      }),
    ).toEqual([
      { field: "fee.enabled", value: false },
      { field: "pool", value: [] },
      { field: "state", value: null },
      { field: "warmup.sessions", value: 0 },
    ]);
  });
});
describe("diagnostic job/result identity", () => {
  const check = (job = independentJob()) =>
    independentStatusProblem(independentSource, job, {
      id: id.run,
      purpose: "diagnostic",
      config_sha256: h.config,
    });
  it("accepts only separate physical success, never source result reused as independent", () =>
    expect(check()).toBeNull());
  it.each(["id", "purpose", "config_sha256"])(
    "rejects changed job %s",
    (field) =>
      expect(check({ ...independentJob(), [field]: "changed" })).not.toBeNull(),
  );
  it.each([
    "evaluation_id",
    "execution_attempt_id",
    "result_manifest_sha256",
    "trace_manifest_sha256",
    "code_sha256",
  ] as const)("rejects changed source %s", (field) => {
    const job = independentJob();
    job.source = { ...job.source, [field]: "changed" };
    expect(check(job)).not.toBeNull();
  });
  it.each([
    "evaluation_id",
    "execution_attempt_id",
    "source_id",
    "code_sha256",
  ] as const)("rejects new result %s disagreement", (field) => {
    const job = independentJob();
    job.result_pin = { ...independentPin, [field]: "changed" };
    expect(check(job)).not.toBeNull();
  });
  it("rejects original physical evidence repackaged as a new independent run", () => {
    const job = independentJob();
    job.result_pin = {
      ...independentPin,
      evaluation_id: independentSource.evaluation_id,
    };
    job.evaluation_id = independentSource.evaluation_id;
    expect(check(job)).not.toBeNull();
    const sameAttempt = independentJob();
    sameAttempt.result_pin = {
      ...independentPin,
      execution_attempt_id: independentSource.execution_attempt_id,
    };
    sameAttempt.active_attempt_id = independentSource.execution_attempt_id;
    expect(check(sameAttempt)).not.toBeNull();
  });
  it("does not promote failed or not-ready results", () => {
    const job = independentJob();
    expect(check({ ...job, status: "failed" })).not.toBeNull();
    expect(check({ ...job, result_ready: false })).not.toBeNull();
  });
  it("rejects stale diagnostic result pins from an earlier physical retry", () =>
    expect(
      diagnosticResultPins(
        { diagnostic_evaluation_id: id.variant },
        independentPin,
      ),
    ).not.toBeNull());
});
describe("controlled protocol and ordered arms", () => {
  const selection = {
    ...verificationSelection,
    controlled_experiment_id: id.control,
    controlled_request_sha256: h.request,
    controlled_protocol_sha256: h.protocol,
  };
  const check = (value = controlledExperiment()) =>
    controlledStatusProblem(independentSource, selection, value);
  it("retains failed variants and exact copied baseline without inventing metrics", () => {
    const value = controlledExperiment();
    expect(check(value)).toBeNull();
    expect(value.arms[1].status).toBe("failed");
    expect(value.arms[1].result_pin).toBeNull();
  });
  it.each(["id", "request_sha256", "protocol_sha256"])(
    "rejects changed controlled %s",
    (field) =>
      expect(
        check({ ...controlledExperiment(), [field]: "changed" }),
      ).not.toBeNull(),
  );
  it("rejects arm reorder, reused run/version and a baseline with changed code", () => {
    for (const mutate of [
      (value: ReturnType<typeof controlledExperiment>) => value.arms.reverse(),
      (value: ReturnType<typeof controlledExperiment>) => {
        value.arms[1].run_id = value.arms[0].run_id;
      },
      (value: ReturnType<typeof controlledExperiment>) => {
        value.arms[1].strategy_version_id = value.arms[0].strategy_version_id;
      },
      (value: ReturnType<typeof controlledExperiment>) => {
        value.arms[0].code_sha256 = h.trace;
      },
    ]) {
      const value = controlledExperiment();
      mutate(value);
      expect(check(value)).not.toBeNull();
    }
  });
  it("never retargets a saved arm to another run or later physical result", () => {
    expect(
      controlledStatusProblem(
        independentSource,
        { ...selection, controlled_arm: 0, controlled_arm_run_id: id.armRun },
        controlledExperiment(),
      ),
    ).not.toBeNull();
    expect(
      controlledStatusProblem(
        independentSource,
        {
          ...selection,
          controlled_arm: 0,
          controlled_arm_run_id: id.run,
          controlled_arm_result_manifest_sha256: h.report,
        },
        controlledExperiment(),
      ),
    ).not.toBeNull();
  });
  it("labels code/multi-factor/unknown parameter changes as not individually validated", () => {
    expect(
      previewArmChanges("source", { minimum: 50 }, "source", { minimum: 50 })
        .interpretation,
    ).toBe("unchanged");
    expect(
      previewArmChanges("source", { minimum: 50 }, "source", { minimum: 100 })
        .interpretation,
    ).toBe("single_parameter_change_not_causal_proof");
    expect(
      previewArmChanges("source", { minimum: 50 }, "source\n# comment", {
        minimum: 50,
      }).interpretation,
    ).toBe("not_individually_validated");
    expect(
      previewArmChanges("source", { minimum: 50 }, "source", {
        minimum: 50,
        unknown: 1,
      }).interpretation,
    ).toBe("not_individually_validated");
    expect(
      previewArmChanges("source", { minimum: 50 }, "source", {}).interpretation,
    ).toBe("not_individually_validated");
    expect(
      previewArmChanges("source", { minimum: 50, max: 1 }, "source", {
        minimum: 100,
        max: 2,
      }).interpretation,
    ).toBe("not_individually_validated");
  });
});
describe("semantic parity is not metrics equality", () => {
  const check = (report = parityReport()) =>
    diagnosticParityProblem(independentSource, independentJob(), {}, report);
  it("binds native reports to the admitted staged evaluation hash, not the independent wrapper hash", () => {
    const job = independentJob(),
      report = parityReport();
    expect(report.replay.binding.execution_config_sha256).not.toBe(
      job.config_sha256,
    );
    expect(report.replay.binding.execution_config_sha256).toBe(
      job.result_pin!.execution_config_sha256,
    );
    expect(
      diagnosticParityProblem(independentSource, job, {}, report),
    ).toBeNull();
    report.replay.binding.execution_config_sha256 = job.config_sha256;
    expect(
      diagnosticParityProblem(independentSource, job, {}, report),
    ).not.toBeNull();
  });
  it("retains portable whole-run binding in addition to the admitted physical result pin", () => {
    const job = independentJob(),
      report = parityReport();
    const { selected_contract: _contract, ...common } =
      job.config as IndependentEvaluationConfig;
    job.config = {
      ...common,
      schema_version: "tradegrad.portable.independent-evaluation.v1",
      evaluation_mode: "portable",
      source_config: portableFixture().body,
      portable_policy_image_digest: `sha256:${"9".repeat(64)}`,
      selected_manifest: {
        id: `${id.strategy}/manifest`,
        sha256: "a".repeat(64),
      },
      selected_parameters: {
        id: `${id.strategy}/parameters`,
        sha256: "b".repeat(64),
      },
    };
    job.result_pin = {
      ...independentPin,
      execution_config_sha256: job.config_sha256,
    };
    report.replay.binding.execution_config_sha256 = job.config_sha256;
    expect(
      diagnosticParityProblem(independentSource, job, {}, report),
    ).toBeNull();
    job.result_pin.execution_config_sha256 = h.evaluationConfig;
    report.replay.binding.execution_config_sha256 = h.evaluationConfig;
    expect(
      diagnosticParityProblem(independentSource, job, {}, report),
    ).not.toBeNull();
  });
  it("requires the complete named check contract rather than a returns-only pass", () => {
    expect(check()).toBeNull();
    const metrics = parityReport();
    metrics.checks = metrics.checks.filter(
      (item) => item.key === "result_metrics",
    );
    expect(check(metrics)).toContain("checks");
    for (const key of requiredDiagnosticChecks) {
      const partial = parityReport();
      partial.checks = partial.checks.filter((item) => item.key !== key);
      expect(check(partial)).not.toBeNull();
    }
  });
  it.each(requiredDiagnosticChecks)(
    "missing %s stays not_verifiable even when result metrics match",
    (key) => {
      const report = parityReport();
      report.checks.find((item) => item.key === key)!.status = "not_verifiable";
      report.outcome = "not_verifiable";
      report.mapping_counts = { unverified_observed_pairing: 1 };
      expect(check(report)).toBeNull();
      expect(diagnosticEvidenceClass(report.outcome)).toContain("Unverified");
    },
  );
  it("mismatch cannot retain a verified-original-event badge", () => {
    const report = parityReport();
    report.checks[0].status = "mismatch";
    report.outcome = "mismatch";
    expect(check(report)).not.toBeNull();
    report.mapping_counts = { unverified_observed_pairing: 1 };
    expect(check(report)).toBeNull();
  });
  it.each([
    "evaluation_id",
    "execution_attempt_id",
    "result_manifest_sha256",
    "code_sha256",
    "execution_config_sha256",
  ] as const)("rejects wrong replay binding %s", (field) => {
    const report = parityReport();
    report.replay.binding[field] = "changed";
    expect(check(report)).not.toBeNull();
  });
  it("rejects source revision/hash mismatch and different saved report", () => {
    const report = parityReport();
    report.source.spec_revision = 2;
    expect(check(report)).not.toBeNull();
    expect(
      diagnosticParityProblem(
        independentSource,
        independentJob(),
        { diagnostic_report_sha256: h.request },
        parityReport(),
      ),
    ).not.toBeNull();
  });
  it("bounds event pages and never uses equal record IDs as cross-run identity", () => {
    expect(parityPageMatches(parityReport(), parityPage(), 0)).toBe(true);
    const sameId = parityPage();
    sameId.items[0].replay_decision_id = sameId.items[0].original_decision_id;
    expect(parityPageMatches(parityReport(), sameId, 0)).toBe(false);
    const missingPreState = parityPage();
    missingPreState.items[0].original_pre_state_sha256 = null;
    expect(parityPageMatches(parityReport(), missingPreState, 0)).toBe(false);
    const mismatchedPreState = parityPage();
    mismatchedPreState.items[0].pre_state_status = "mismatch";
    expect(parityPageMatches(parityReport(), mismatchedPreState, 0)).toBe(
      false,
    );
    expect(
      parityPageMatches(
        parityReport(),
        { ...parityPage(), report_sha256: h.trace },
        0,
      ),
    ).toBe(false);
    expect(parityPageMatches(parityReport(), parityPage(), 25)).toBe(false);
    expect(
      parityPageMatches(parityReport(), { ...parityPage(), total: 2 }, 0),
    ).toBe(false);
  });
  it("replay drilldown retains exact physical and source/spec pins for each distinct event", () => {
    const original = parityReplayTarget(
      parityReport().source,
      "original-event",
    )!;
    const replay = parityReplayTarget(parityReport().replay, "new-event")!;
    expect(original.search.result_manifest_sha256).toBe(hashes.result);
    expect(replay.search.result_manifest_sha256).toBe(h.result);
    expect(replay.search.evaluation_id).toBe(id.evaluation);
    expect(original.search.decision_id).toBe("original-event");
    expect(replay.search.decision_id).toBe("new-event");
    expect(
      parityReplayTarget(
        { ...parityReport().source, trace_manifest_sha256: null },
        "event",
      ),
    ).toBeNull();
  });
  it("exposes missing original RNG/environment/coverage instead of inferred replayability", () => {
    const value = capability();
    value.can_execute = false;
    value.unavailable_reasons = [
      "original_environment_missing",
      "original_rng_missing",
    ];
    expect(value.unavailable_reasons).toHaveLength(2);
  });
});
