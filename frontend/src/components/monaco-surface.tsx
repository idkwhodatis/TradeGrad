import "@/lib/monaco";
import { Editor, DiffEditor } from "@monaco-editor/react";

export type MonacoSurfaceProps = {
  code?: string;
  original?: string;
  modified?: string;
  onChange?: (code: string) => void;
  label: string;
  height?: string;
  readOnly?: boolean;
};
export default function MonacoSurface({
  code,
  original,
  modified,
  onChange,
  label,
  height = "360px",
  readOnly = true,
}: MonacoSurfaceProps) {
  const options = {
    readOnly,
    domReadOnly: readOnly,
    automaticLayout: true,
    accessibilitySupport: "on" as const,
    ariaLabel: label,
    minimap: { enabled: false },
    scrollBeyondLastLine: false,
  };
  return original !== undefined && modified !== undefined ? (
    <DiffEditor
      height={height}
      language="python"
      original={original}
      modified={modified}
      options={{
        ...options,
        readOnly: true,
        domReadOnly: true,
        originalEditable: false,
        renderSideBySide: false,
      }}
    />
  ) : (
    <Editor
      height={height}
      language="python"
      value={code}
      onChange={(value) => onChange?.(value ?? "")}
      options={options}
    />
  );
}
