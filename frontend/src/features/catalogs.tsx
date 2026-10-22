import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Braces, Database, Code2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  DemoNotice,
  ErrorState,
  Loading,
  PageHeader,
} from "@/components/common";
import { datasetsOptions, strategiesOptions, strategyOptions } from "@/lib/api";
import { formatDate, taskLabels } from "@/lib/format";

export function DatasetsPage() {
  const datasets = useQuery(datasetsOptions);
  return (
    <>
      <PageHeader
        title="Dataset snapshots"
        subtitle="Immutable inputs, with their provenance and date boundaries intact."
      />
      <DemoNotice />
      <div className="mt-6">
        {datasets.isPending ? (
          <Loading />
        ) : datasets.error ? (
          <ErrorState
            error={datasets.error}
            retry={() => void datasets.refetch()}
          />
        ) : (
          <div className="catalog">
            {datasets.data?.data.map((dataset) => (
              <article className="panel panel-body" key={dataset.id}>
                <div className="flex items-start justify-between gap-3">
                  <div className="catalog-icon">
                    <Database size={21} />
                  </div>
                  <Badge variant="secondary">
                    {dataset.is_synthetic
                      ? "Synthetic demo"
                      : "Published snapshot"}
                  </Badge>
                </div>
                <h2>{dataset.name}</h2>
                <p className="mt-2 text-xs muted">
                  {dataset.market} market · {dataset.source}
                </p>
                <dl className="details">
                  <div>
                    <dt>Coverage</dt>
                    <dd>
                      {dataset.start_date} → {dataset.end_date}
                    </dd>
                  </div>
                  <div>
                    <dt>Date convention</dt>
                    <dd>[start, end) · exclusive end</dd>
                  </div>
                  <div>
                    <dt>Published</dt>
                    <dd>{formatDate(dataset.created_at)} UTC</dd>
                  </div>
                  <div>
                    <dt>Content SHA-256</dt>
                    <dd className="hash">{dataset.content_sha256}</dd>
                  </div>
                </dl>
                <details className="mt-5">
                  <summary className="text-link cursor-pointer">
                    View snapshot manifest
                  </summary>
                  <pre className="preview mt-3">
                    {JSON.stringify(dataset.manifest, null, 2)}
                  </pre>
                </details>
              </article>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
export function StrategiesPage() {
  const strategies = useQuery(strategiesOptions);
  const [selected, setSelected] = useState<string | null>(null);
  const detail = useQuery({
    ...strategyOptions(selected || ""),
    enabled: !!selected,
  });
  return (
    <>
      <PageHeader
        title="Strategy library"
        subtitle="Four original seeds. Versioned by content, never modified in place."
      />
      <DemoNotice />
      <div className="mt-6">
        {strategies.isPending ? (
          <Loading />
        ) : strategies.error ? (
          <ErrorState
            error={strategies.error}
            retry={() => void strategies.refetch()}
          />
        ) : (
          <div className="catalog">
            {strategies.data?.data.map((strategy) => (
              <article className="panel panel-body" key={strategy.id}>
                <div className="flex items-start justify-between">
                  <div className="catalog-icon">
                    <Braces size={21} />
                  </div>
                  <Badge variant="outline">native_akquant</Badge>
                </div>
                <h2>{strategy.name}</h2>
                <p className="mt-2 text-xs muted">
                  {taskLabels[strategy.task]} ·{" "}
                  {strategy.name.startsWith("E2E ")
                    ? "E2E test fixture, not an original seed"
                    : "Unchanged upstream seed"}
                </p>
                <dl className="details">
                  <div>
                    <dt>Parent version</dt>
                    <dd>{strategy.parent_id || "Original seed"}</dd>
                  </div>
                  <div>
                    <dt>Code SHA-256</dt>
                    <dd className="hash">{strategy.content_sha256}</dd>
                  </div>
                </dl>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-5"
                  onClick={() =>
                    setSelected(selected === strategy.id ? null : strategy.id)
                  }
                >
                  <Code2 />
                  {selected === strategy.id ? "Hide source" : "View source"}
                </Button>
                {selected === strategy.id && (
                  <section
                    className="mt-4"
                    aria-label={`${strategy.name} source`}
                  >
                    <div className="flex justify-between items-center mb-2">
                      <span className="text-xs muted">
                        Read-only strategy source
                      </span>
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        aria-label="Close source"
                        onClick={() => setSelected(null)}
                      >
                        <X />
                      </Button>
                    </div>
                    {detail.isPending ? (
                      <Loading text="Loading strategy source…" />
                    ) : detail.error ? (
                      <ErrorState error={detail.error} />
                    ) : (
                      <pre className="preview" tabIndex={0}>
                        {detail.data?.code}
                      </pre>
                    )}
                  </section>
                )}
              </article>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
