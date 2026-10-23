// Emit no raw test output or credential-bearing failure objects. A clean exit
// requires exactly the intended case to pass once, not a skipped/no-test run.
module.exports = class OperatorProfileReporter {
  onBegin(_config, suite) {
    this.tests = suite.allTests();
  }

  async onEnd(result) {
    const tests = this.tests || [];
    const passed =
      result.status === "passed" &&
      tests.length === 1 &&
      tests[0].title.includes("@operator-model-profile") &&
      tests[0].results.length === 1 &&
      tests[0].results[0].status === "passed";
    return { status: passed ? "passed" : "failed" };
  }
};
