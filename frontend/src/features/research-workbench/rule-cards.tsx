import type { DecisionDetail, StrategySpec } from "@/client/types.gen";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { recordedValue, ruleObservation } from "@/lib/workbench-evidence";

export function RuleCards({
  spec,
  selectedRule,
  detail,
  onSelect,
}: {
  spec: StrategySpec;
  selectedRule?: string;
  detail?: DecisionDetail;
  onSelect: (ruleId: string) => void;
}) {
  return (
    <div className="workbench-rules">
      {spec.rules.map((rule) => {
        const observation = ruleObservation(rule, detail);
        const selected = selectedRule === rule.rule_id;
        const features =
          selected && observation.stage
            ? detail?.features.items?.filter(
                (row) => row.stage_id === rule.stage_id,
              )
            : undefined;
        const predicates =
          selected && observation.stage
            ? detail?.predicates.items?.filter(
                (row) => row.stage_id === rule.stage_id,
              )
            : undefined;
        return (
          <article
            key={rule.rule_id}
            className="workbench-rule"
            data-testid={`rule-${rule.rule_id}`}
            data-selected={selected}
          >
            <div className="flex flex-wrap gap-2">
              <Badge variant="outline">{rule.category}</Badge>
              <Badge variant="secondary">{rule.source}</Badge>
              <Badge variant="outline">{rule.mapping}</Badge>
            </div>
            <h3 className="font-semibold mt-3">{rule.title}</h3>
            <p className="text-sm mt-2 whitespace-pre-wrap">
              {rule.description}
            </p>
            <dl>
              <dt>Parameters</dt>
              <dd>{recordedValue(rule.parameters)}</dd>
              <dt>Units</dt>
              <dd>{recordedValue(rule.units)}</dd>
              <dt>Inputs</dt>
              <dd>{rule.inputs.join(", ") || "None declared"}</dd>
              <dt>Outputs</dt>
              <dd>{rule.outputs.join(", ") || "None declared"}</dd>
              <dt>Stage</dt>
              <dd>{rule.stage_id || "No mapped stage"}</dd>
              <dt>Source</dt>
              <dd>
                {rule.source_ref.file}:{rule.source_ref.line_start}–
                {rule.source_ref.line_end} · {rule.source_ref.symbol}
              </dd>
            </dl>
            <Button
              variant="outline"
              size="sm"
              onClick={() => onSelect(rule.rule_id)}
              aria-label={`Inspect rule: ${rule.title}`}
            >
              Inspect rule and source
            </Button>
            <p className="text-xs muted mt-3">{observation.status}</p>
            {selected && observation.stage && (
              <section
                aria-label="Actual selected stage observations"
                className="mt-3"
              >
                <h4 className="text-sm font-semibold">
                  Actual selected stage observations
                </h4>
                <p className="text-xs break-all">
                  Decision: {detail!.decision!.decision_id} · Instrument:{" "}
                  {detail!.instrument_id || "Whole decision"}
                </p>
                <p className="text-xs">
                  Saved: {observation.stage.features_saved} features ·{" "}
                  {observation.stage.predicates_saved} conditions. Loaded page:{" "}
                  {features?.length ?? 0} features of{" "}
                  {detail!.features.total ?? "unknown"},{" "}
                  {predicates?.length ?? 0} conditions of{" "}
                  {detail!.predicates.total ?? "unknown"}. Other records are not
                  inferred.
                </p>
                {!features?.length && (
                  <p className="text-xs muted">
                    No feature values on this loaded page.
                  </p>
                )}
                {features?.map(({ observation: value }, index) => (
                  <div
                    className="mt-2 text-xs"
                    key={`${value.feature_id}-${index}`}
                  >
                    <strong>{value.feature_id}</strong>:{" "}
                    {recordedValue(value.value)} {value.unit} · {value.validity}
                    <br />
                    Information cutoff: {value.information_cutoff} ·{" "}
                    {value.source}
                    {value.missing_reason && <p>{value.missing_reason}</p>}
                  </div>
                ))}
                {!predicates?.length && (
                  <p className="text-xs muted">
                    No condition outcomes on this loaded page. Absence is not
                    false.
                  </p>
                )}
                {predicates?.map(({ observation: value }, index) => (
                  <div
                    className="mt-2 text-xs"
                    key={`${value.condition_id}-${index}`}
                  >
                    <strong>{value.condition_id}</strong>: {value.state}
                    <br />
                    Operands: {recordedValue(value.operands)} · Operator:{" "}
                    {value.operator} · Threshold:{" "}
                    {recordedValue(value.threshold)} · {value.source}
                    {value.missing_reason && <p>{value.missing_reason}</p>}
                  </div>
                ))}
              </section>
            )}
          </article>
        );
      })}
    </div>
  );
}
