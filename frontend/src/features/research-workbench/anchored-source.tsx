import { Component, Suspense, lazy, type ReactNode } from "react";
import type { SourceRef } from "@/client/types.gen";
import { Panel } from "@/components/common";
const SourceEditor = lazy(() => import("./source-editor"));
class SourceBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <p className="notice">
        The enhanced editor is unavailable. Exact source and selected lines
        remain below.
      </p>
    ) : (
      this.props.children
    );
  }
}
export function AnchoredSource({
  code,
  codeSha256,
  anchor,
}: {
  code: string;
  codeSha256: string;
  anchor?: SourceRef | null;
}) {
  const valid =
    !anchor ||
    (anchor.code_sha256 === codeSha256 &&
      anchor.line_start >= 1 &&
      anchor.line_end >= anchor.line_start &&
      anchor.line_end <= code.split(/\r\n|\n|\r/).length);
  return (
    <Panel title="Exact source and code anchor">
      <p className="text-xs break-all" data-testid="source-code-hash">
        Source code SHA-256: {codeSha256}
      </p>
      {!valid ? (
        <p role="alert" className="notice error">
          This source anchor does not match the selected immutable source. No
          replacement location was used.
        </p>
      ) : (
        <>
          <p className="muted my-3">
            {anchor
              ? `${anchor.file} · ${anchor.symbol} · lines ${anchor.line_start}–${anchor.line_end}`
              : "Select a rule to locate its exact source lines."}
          </p>
          <SourceBoundary>
            <Suspense
              fallback={<p role="status">Loading local source editor…</p>}
            >
              <SourceEditor key={codeSha256} code={code} anchor={anchor} />
            </Suspense>
          </SourceBoundary>
          {anchor && (
            <section aria-label="Selected source lines" className="mt-3">
              <h3>Selected source lines</h3>
              <pre className="preview" tabIndex={0}>
                {code
                  .split(/\r\n|\n|\r/)
                  .slice(anchor.line_start - 1, anchor.line_end)
                  .map((line, index) => `${anchor.line_start + index}: ${line}`)
                  .join("\n")}
              </pre>
            </section>
          )}
        </>
      )}
      <details className="mt-4">
        <summary>Accessible exact source</summary>
        <pre className="preview" tabIndex={0}>
          {code}
        </pre>
      </details>
    </Panel>
  );
}
