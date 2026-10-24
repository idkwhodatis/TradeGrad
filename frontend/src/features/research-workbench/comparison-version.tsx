import type { ComparisonManifest } from "@/client/types.gen";

export const currentComparisonAlgorithm = "tradegrad.observed-comparison.v2";

export function ComparisonVersion({
  version,
}: {
  version: ComparisonManifest["algorithm_version"];
}) {
  return (
    <div data-testid="comparison-algorithm">
      <p>Comparison algorithm: {version ?? "Not recorded"}</p>
      {version !== currentComparisonAlgorithm && (
        <p className="notice mt-3" role="note">
          Historical comparison semantics. Saved rows, conclusions and evidence
          pins are unchanged. This version could label uncollected strategy
          details as observed absence; those claims are not certified by the
          corrected algorithm. Inspect original coverage and treat unsupported
          absence claims as unknown. Retrying this version is unavailable. To
          compare with current semantics, explicitly submit a new comparison;
          existing notes and links will not be retargeted.
        </p>
      )}
    </div>
  );
}
