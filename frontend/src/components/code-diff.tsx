import { CodeDiff } from "@/components/code-editor";

// The shared editor configures a bundled, self-hosted Monaco worker. This module
// is lazy-loaded by the research page, never through a public CDN.
export default function ResearchCodeDiff({
  original,
  modified,
}: {
  original: string;
  modified: string;
}) {
  return (
    <CodeDiff
      original={original}
      modified={modified}
      label="Read-only parent and candidate Python diff"
    />
  );
}
