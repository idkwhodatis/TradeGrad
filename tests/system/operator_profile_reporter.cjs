// Emit no raw test output or credential-bearing failure objects. A clean exit
// requires exactly the intended case to pass once, not a skipped/no-test run.
module.exports = class OperatorProfileReporter {
  onBegin(_config, suite) {
    this.tests = suite.allTests();
  }

  async onEnd(result) {
    const tests = this.tests || [];
    const selected = {
      native_akquant: [
        "configured-research.spec.ts",
        "@operator-model-profile",
      ],
      portable: ["portable-research.spec.ts", "@portable-operator-profile"],
    }[process.env.E2E_MODEL_BROWSER_MODE];
    const passed =
      Array.isArray(selected) &&
      result.status === "passed" &&
      tests.length === 1 &&
      tests[0].title.includes(selected[1]) &&
      tests[0].location?.file
        ?.replaceAll("\\", "/")
        .endsWith("/tests/e2e/" + selected[0]) &&
      tests[0].results.length === 1 &&
      tests[0].results[0].status === "passed";
    return { status: passed ? "passed" : "failed" };
  }
};
