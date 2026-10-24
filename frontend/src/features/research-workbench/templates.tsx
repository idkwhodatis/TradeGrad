import { useQuery } from "@tanstack/react-query";
import type { StrategyCreate } from "@/client/types.gen";
import { ErrorState, Loading, Panel } from "@/components/common";
import { Button } from "@/components/ui/button";
import { researchTemplatesOptions } from "@/lib/workbench-api";
export function ControlledTemplates({
  onUse,
}: {
  onUse: (value: StrategyCreate) => void;
}) {
  const templates = useQuery(researchTemplatesOptions);
  return (
    <div className="mb-5">
      <Panel title="Controlled research templates">
        <p className="text-sm muted mb-3">
          Start with one of two maintained templates to inspect source-bound
          rules and a read-only graph. Choosing a template opens an editable
          draft; saving creates a new immutable strategy, not a backtest.
          Editing its code removes exact-template graph eligibility.
        </p>
        {templates.isPending ? (
          <Loading text="Loading maintained templates…" />
        ) : templates.error ? (
          <ErrorState error={templates.error} />
        ) : (
          <div className="flex flex-wrap gap-3">
            {templates.data?.data.map((template) => (
              <Button
                key={template.template_id}
                variant="outline"
                onClick={() =>
                  onUse({
                    name: `${template.name} template`,
                    task:
                      template.task_suffix === "time" ? "cn_time" : "cn_cross",
                    code: template.code,
                    manifest: template.manifest,
                  })
                }
              >
                {template.template_id === "daily_timing"
                  ? "Use Daily timing template"
                  : "Use Cross-sectional template"}
              </Button>
            ))}
          </div>
        )}
      </Panel>
    </div>
  );
}
