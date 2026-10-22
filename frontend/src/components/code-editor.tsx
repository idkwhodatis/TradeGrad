import { Component, Suspense, lazy, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";

const MonacoSurface = lazy(() => import("./monaco-surface"));
class EditorBoundary extends Component<
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
        The enhanced editor could not load. Use the plain source below.
      </p>
    ) : (
      this.props.children
    );
  }
}
function EnhancedEditor({ children }: { children: ReactNode }) {
  return (
    <EditorBoundary>
      <Suspense
        fallback={
          <p role="status" className="muted">
            Loading local code editor…
          </p>
        }
      >
        {children}
      </Suspense>
    </EditorBoundary>
  );
}
export function CodeViewer({
  code,
  label = "Read-only Python source",
  height,
}: {
  code: string;
  label?: string;
  height?: string;
}) {
  return (
    <section aria-label={label}>
      <EnhancedEditor>
        <MonacoSurface code={code} label={label} height={height} />
      </EnhancedEditor>
      <details className="mt-3">
        <summary className="text-link">Accessible source</summary>
        <pre className="preview" tabIndex={0}>
          {code}
        </pre>
      </details>
    </section>
  );
}
export function CodeDiff({
  original,
  modified,
  label = "Read-only parent and version diff",
  height,
}: {
  original: string;
  modified: string;
  label?: string;
  height?: string;
}) {
  return (
    <section aria-label={label}>
      <EnhancedEditor>
        <MonacoSurface
          original={original}
          modified={modified}
          label={label}
          height={height || "440px"}
        />
      </EnhancedEditor>
      <details className="mt-3">
        <summary className="text-link">Accessible original source</summary>
        <pre className="preview" tabIndex={0}>
          {original || "No parent source."}
        </pre>
      </details>
      <details className="mt-3">
        <summary className="text-link">Accessible modified source</summary>
        <pre className="preview" tabIndex={0}>
          {modified}
        </pre>
      </details>
    </section>
  );
}
export function CodeEditor({
  code,
  onChange,
  disabled = false,
  label = "Python source",
}: {
  code: string;
  onChange: (code: string) => void;
  disabled?: boolean;
  label?: string;
}) {
  const [enhanced, setEnhanced] = useState(false);
  return (
    <div>
      <div className="flex gap-3 items-center mb-2">
        <span className="text-sm">{label}</span>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setEnhanced(!enhanced)}
        >
          {enhanced ? "Close enhanced editor" : "Open enhanced editor"}
        </Button>
      </div>
      {enhanced && (
        <EnhancedEditor>
          <MonacoSurface
            code={code}
            onChange={onChange}
            label={`${label} enhanced editor`}
            readOnly={disabled}
          />
        </EnhancedEditor>
      )}
      <label className="block mt-3 text-xs muted" htmlFor="strategy-source">
        {enhanced
          ? "Plain-text editor (accessible fallback)"
          : "Plain-text editor"}
      </label>
      <textarea
        id="strategy-source"
        aria-label={label}
        className="preview w-full"
        rows={14}
        spellCheck={false}
        value={code}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}
