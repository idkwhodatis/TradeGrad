import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { expect, it } from "vitest";
import { UploadEvidenceNotice } from "../../src/components/data-common";

it("visibly preserves the persisted unverified upload assumption", () => {
  const html = renderToString(
    createElement(UploadEvidenceNotice, {
      evidence: "user_declaration_unverified",
    }),
  );
  expect(html).toContain('data-testid="upload-evidence-notice"');
  expect(html).toContain("not independently verified");
  expect(html).toContain("not provider,");
  expect(html).toContain("runtime or cross-framework certification");
  expect(html).not.toContain("<details");
});

it.each([undefined, null, "unknown", "uploaded", "synthetic", "verified"])(
  "does not infer a raw/no-actions declaration from %s",
  (evidence) => {
    expect(
      renderToString(createElement(UploadEvidenceNotice, { evidence })),
    ).toBe("");
  },
);
