import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Braces, Code2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  DemoNotice,
  ErrorState,
  Loading,
  PageHeader,
} from "@/components/common";
import { strategiesOptions, strategyOptions } from "@/lib/api";
import { taskLabels } from "@/lib/format";

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
