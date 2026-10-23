import type { BenchmarkConfig, DatasetPublic } from "@/client/types.gen";
import {
  benchmarkKey,
  benchmarkOptions,
  benchmarkSelectionIssue,
} from "@/lib/benchmarks";
import { Field, Metadata } from "./data-common";

export function BenchmarkSelector({
  value,
  datasets,
  market,
  disabled,
  onChange,
}: {
  value: BenchmarkConfig | null | undefined;
  datasets: DatasetPublic[];
  market: string;
  disabled?: boolean;
  onChange: (value: BenchmarkConfig | null) => void;
}) {
  const options = benchmarkOptions(datasets, market);
  const error = benchmarkSelectionIssue(value, datasets, market);
  return (
    <div className="stack" data-testid="benchmark-selector">
      <Field
        id="benchmark-input"
        label="Benchmark snapshot and listing (optional)"
        help="Explicit immutable selection only. Raw daily close-price change, not dividends, total return or a cost-matched portfolio. Strict server preflight checks exact scored sessions, calendar version, timezone, currency and known price semantics."
      >
        <select
          id="benchmark-input"
          value={benchmarkKey(value)}
          disabled={disabled}
          onChange={(event) => {
            const choice = options.find(
              (item) => item.key === event.target.value,
            );
            onChange(choice?.value ?? null);
          }}
        >
          <option value="">None · no comparison requested</option>
          {error && (
            <option value={benchmarkKey(value)}>
              Unavailable pinned benchmark
            </option>
          )}
          {options.map((item) => (
            <option key={item.key} value={item.key}>
              {item.label}
            </option>
          ))}
        </select>
      </Field>
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
      {value && (
        <Metadata
          values={{
            "Benchmark dataset version": value.dataset.id,
            "Benchmark dataset SHA-256": value.dataset.sha256,
            "Benchmark listing": value.listing_id,
            Definition: value.definition_version,
          }}
        />
      )}
      <p className="notice">
        Comparison starts at the first actual scored-session close and observed
        account equity, never fabricated starting capital. Benchmark prices are
        not fed to optimization or strategy code. Holdout comparison is only
        computed in a separately authorized final evaluation.
      </p>
    </div>
  );
}
