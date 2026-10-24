import { describe, expect, it } from "vitest";
import {
  changeSelection,
  clearedControlledArmSelection,
  clearedControlledSelection,
  clearedDiagnosticSelection,
  selectionProblem,
  workbenchSearch,
} from "../../src/lib/workbench-selection";
import {
  verificationHashes as h,
  verificationIds as id,
  verificationSelection,
} from "./verification-fixtures";
const selection = {
  ...verificationSelection,
  diagnostic_run_id: id.run,
  diagnostic_config_sha256: h.config,
  diagnostic_report_sha256: h.report,
  diagnostic_event_offset: 25,
  diagnostic_evaluation_id: id.evaluation,
  diagnostic_execution_attempt_id: id.attempt,
  diagnostic_result_manifest_sha256: h.result,
  diagnostic_trace_manifest_sha256: h.trace,
  controlled_experiment_id: id.control,
  controlled_request_sha256: h.request,
  controlled_protocol_sha256: h.protocol,
  controlled_arm: 0,
  controlled_arm_run_id: id.run,
  controlled_arm_evaluation_id: id.evaluation,
  controlled_arm_execution_attempt_id: id.attempt,
  controlled_arm_result_manifest_sha256: h.result,
  controlled_arm_trace_manifest_sha256: h.trace,
};
describe("independent URL evidence identities", () => {
  it("round trips all job/report/arm pins without replacing original replay identities", () => {
    const query = Object.fromEntries(
      Object.entries(selection)
        .filter(([, value]) => value !== undefined)
        .map(([key, value]) => [key, String(value)]),
    );
    expect(workbenchSearch(query)).toEqual(selection);
    expect(selectionProblem(selection)).toBeNull();
    expect(selection.evaluation_id).not.toBe(
      selection.diagnostic_evaluation_id,
    );
    expect(changeSelection(selection, { tab: "description" })).toEqual({
      ...selection,
      tab: "description",
    });
  });
  it.each([
    "diagnostic_config_sha256",
    "diagnostic_report_sha256",
    "diagnostic_result_manifest_sha256",
    "controlled_request_sha256",
    "controlled_protocol_sha256",
    "controlled_arm_trace_manifest_sha256",
  ])("rejects malformed %s rather than dropping it", (key) =>
    expect(() =>
      workbenchSearch({ ...selection, [key]: "old-hash" }),
    ).toThrow(),
  );
  it.each([-1, 3, 0.5, "", "latest", null])(
    "rejects invalid controlled arm %s",
    (arm) => expect(() => workbenchSearch({ controlled_arm: arm })).toThrow(),
  );
  it.each([
    { diagnostic_report_sha256: h.report },
    { diagnostic_run_id: id.run, diagnostic_report_sha256: h.report },
    { controlled_protocol_sha256: h.protocol },
    { controlled_experiment_id: id.control, controlled_arm: 0 },
    { controlled_experiment_id: id.control, controlled_arm_run_id: id.run },
    { diagnostic_run_id: id.run, diagnostic_execution_attempt_id: id.attempt },
    { diagnostic_run_id: id.run, diagnostic_evaluation_id: id.evaluation },
    { controlled_arm_evaluation_id: id.evaluation },
  ])("refuses incomplete derived pins %j", (pins) =>
    expect(selectionProblem(pins)).not.toBeNull(),
  );
  it("clears only requested downstream identity, preserving other evidence and original observations", () => {
    const diagnostic = changeSelection(selection, clearedDiagnosticSelection());
    expect(diagnostic.diagnostic_run_id).toBeUndefined();
    expect(diagnostic.controlled_experiment_id).toBe(id.control);
    expect(diagnostic.evaluation_id).toBe(selection.evaluation_id);
    const arm = changeSelection(selection, clearedControlledArmSelection());
    expect(arm.controlled_arm).toBeUndefined();
    expect(arm.controlled_protocol_sha256).toBe(h.protocol);
    expect(arm.diagnostic_report_sha256).toBe(h.report);
    const controls = changeSelection(selection, clearedControlledSelection());
    expect(controls.controlled_experiment_id).toBeUndefined();
    expect(controls.diagnostic_run_id).toBe(id.run);
  });
});
