import "@/lib/monaco";
import { useEffect, useRef } from "react";
import { Editor } from "@monaco-editor/react";
import type { editor } from "monaco-editor";
import type { SourceRef } from "@/client/types.gen";

export default function SourceEditor({
  code,
  anchor,
}: {
  code: string;
  anchor?: SourceRef | null;
}) {
  const instance = useRef<editor.IStandaloneCodeEditor | null>(null);
  const highlight = useRef<editor.IEditorDecorationsCollection | null>(null);
  function reveal(target: editor.IStandaloneCodeEditor) {
    highlight.current?.clear();
    if (!anchor) return;
    const range = {
      startLineNumber: anchor.line_start,
      startColumn: 1,
      endLineNumber: anchor.line_end,
      endColumn: 1,
    };
    highlight.current = target.createDecorationsCollection([
      {
        range,
        options: { isWholeLine: true, className: "workbench-source-highlight" },
      },
    ]);
    target.setSelection(range);
    target.revealLinesInCenter(anchor.line_start, anchor.line_end);
  }
  useEffect(() => {
    if (instance.current) reveal(instance.current);
  }, [anchor]);
  return (
    <Editor
      height="380px"
      language="python"
      value={code}
      onMount={(target) => {
        instance.current = target;
        reveal(target);
      }}
      options={{
        readOnly: true,
        domReadOnly: true,
        automaticLayout: true,
        accessibilitySupport: "on",
        ariaLabel: "Exact immutable strategy source",
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
      }}
    />
  );
}
