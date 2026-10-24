import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { WorkbenchEvaluation, WorkbenchSpec } from "@/client/types.gen";
import { ErrorState, Loading } from "@/components/common";
import { Metadata, Pagination } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { strategyOptions } from "@/lib/api";
import { candidateOptions } from "@/lib/research-api";
import { comparisonStrategyOptions } from "@/lib/comparison-api";
import {
  comparisonLeafProblem,
  sideFromEvidence,
  sideSelectionProblem,
  sideTitle,
} from "@/lib/comparison-evidence";
import {
  workbenchEvaluationOptions,
  workbenchEvaluationsOptions,
  workbenchSpecOptions,
  type SourceResource,
} from "@/lib/workbench-api";
import {
  comparisonSidePatch,
  comparisonSideSelection,
  clearComparisonResult,
  clearComparisonSide,
  type ComparisonSide,
  type WorkbenchSelection,
} from "@/lib/workbench-selection";
export function ComparisonSourcePicker({
  side,
  selection,
  update,
  currentSpec,
  currentLeaf,
  currentVerified,
  disabled,
}: {
  side: ComparisonSide;
  selection: WorkbenchSelection;
  update: (patch: Partial<WorkbenchSelection>) => void;
  currentSpec: WorkbenchSpec;
  currentLeaf?: WorkbenchEvaluation;
  currentVerified: boolean;
  disabled?: boolean;
}) {
  const pins = comparisonSideSelection(selection, side);
  const title = sideTitle(side);
  const [kind, setKind] = useState<SourceResource["kind"]>(
    pins.source_kind || "strategy_version",
  );
  const [candidateId, setCandidateId] = useState(
    pins.source_kind === "candidate" ? pins.source_id || "" : "",
  );
  useEffect(() => {
    if (pins.source_kind) setKind(pins.source_kind);
    setLeafOffset(0);
    setCandidateId(
      pins.source_kind === "candidate" ? pins.source_id || "" : "",
    );
  }, [pins.source_kind, pins.source_id]);
  const [offset, setOffset] = useState(0);
  const [leafOffset, setLeafOffset] = useState(0);
  const [inputError, setInputError] = useState<unknown>(null);
  const source: SourceResource = {
    kind: pins.source_kind || kind,
    id: pins.source_id || "",
  };
  const problem = sideSelectionProblem(pins);
  const catalog = useQuery(comparisonStrategyOptions(offset));
  const strategy = useQuery({
    ...strategyOptions(source.id),
    enabled: Boolean(
      source.id && source.kind === "strategy_version" && !problem,
    ),
  });
  const candidate = useQuery({
    ...candidateOptions(source.id),
    enabled: Boolean(source.id && source.kind === "candidate" && !problem),
  });
  const spec = useQuery({
    ...workbenchSpecOptions(source, {
      spec_revision: pins.spec_revision,
      spec_sha256: pins.spec_sha256,
    }),
    enabled: Boolean(source.id && !problem),
  });
  const leaves = useQuery({
    ...workbenchEvaluationsOptions(source, leafOffset),
    enabled: Boolean(source.id && !problem),
  });
  const leaf = useQuery({
    ...workbenchEvaluationOptions(pins.evaluation_id),
    enabled: Boolean(pins.evaluation_id && !problem),
  });
  const hash =
    source.kind === "candidate"
      ? candidate.data?.code_sha256
      : strategy.data?.content_sha256;
  const specProblem =
    spec.data &&
    (spec.data.resource_kind !== source.kind ||
      spec.data.resource_id !== source.id ||
      (hash && hash !== spec.data.code_sha256) ||
      (pins.code_sha256 && pins.code_sha256 !== spec.data.code_sha256) ||
      (pins.strategy_version_id &&
        pins.strategy_version_id !== spec.data.strategy_version_id))
      ? "Returned source or description differs from this exact side selection. No replacement was used."
      : null;
  const leafProblem = leaf.data ? comparisonLeafProblem(pins, leaf.data) : null;
  useEffect(() => {
    if (
      spec.data &&
      hash &&
      !problem &&
      !specProblem &&
      (!pins.spec_revision || !pins.strategy_version_id || !pins.code_sha256)
    )
      update(
        comparisonSidePatch(side, { ...pins, ...sideFromEvidence(spec.data) }),
      );
  }, [
    spec.data,
    hash,
    problem,
    specProblem,
    pins.spec_revision,
    pins.strategy_version_id,
    pins.code_sha256,
    source.id,
    side,
    update,
  ]);
  function chooseSource(value: SourceResource) {
    setKind(value.kind);
    setLeafOffset(0);
    setInputError(null);
    update({
      ...clearComparisonSide(side),
      ...clearComparisonResult(),
      ...comparisonSidePatch(side, {
        source_kind: value.kind,
        source_id: value.id,
      }),
    });
  }
  function chooseLeaf(value?: WorkbenchEvaluation) {
    if (!spec.data || specProblem || !hash) return;
    try {
      update({
        ...clearComparisonSide(side),
        ...clearComparisonResult(),
        ...comparisonSidePatch(side, sideFromEvidence(spec.data, value)),
      });
    } catch (error) {
      setInputError(error);
    }
  }
  const choices = leaves.data?.data || [];
  const allLeaves =
    leaf.data &&
    !choices.some((item) => item.evaluation_id === leaf.data!.evaluation_id)
      ? [leaf.data, ...choices]
      : choices;
  const strategyChoices = catalog.data?.data || [];
  const allStrategies =
    strategy.data &&
    !strategyChoices.some((item) => item.id === strategy.data!.id)
      ? [strategy.data, ...strategyChoices]
      : strategyChoices;
  return (
    <fieldset
      aria-label={`${title} comparison source`}
      className="workbench-rule"
      disabled={disabled}
    >
      <legend>{title} immutable source and leaf</legend>
      <Button
        type="button"
        variant="outline"
        onClick={() =>
          update({
            ...clearComparisonSide(side),
            ...clearComparisonResult(),
            ...comparisonSidePatch(
              side,
              sideFromEvidence(
                currentSpec,
                currentVerified ? currentLeaf : undefined,
              ),
            ),
          })
        }
      >
        Use current selection as {title}
      </Button>
      <label className="workbench-field">
        {title} source type
        <select
          value={kind}
          onChange={(event) => {
            const next = event.target.value as SourceResource["kind"];
            setKind(next);
            update({
              ...clearComparisonSide(side),
              ...clearComparisonResult(),
            });
          }}
        >
          <option value="strategy_version">Owned saved strategy</option>
          <option value="candidate">Owned candidate</option>
        </select>
      </label>
      {kind === "strategy_version" ? (
        <>
          <label className="workbench-field">
            {title} saved strategy
            <select
              value={
                pins.source_kind === "strategy_version"
                  ? pins.source_id || ""
                  : ""
              }
              onChange={(event) => {
                if (event.target.value)
                  chooseSource({
                    kind: "strategy_version",
                    id: event.target.value,
                  });
                else
                  update({
                    ...clearComparisonSide(side),
                    ...clearComparisonResult(),
                  });
              }}
            >
              <option value="">Choose an owned source explicitly</option>
              {allStrategies.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name} · {item.id}
                </option>
              ))}
            </select>
          </label>
          {catalog.data && (
            <Pagination
              offset={offset}
              limit={25}
              count={catalog.data.count}
              busy={catalog.isFetching}
              onPage={setOffset}
            />
          )}
          {catalog.error && <ErrorState error={catalog.error} />}
        </>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (!/^[a-f0-9-]{36}$/i.test(candidateId.trim())) {
              setInputError(
                new Error(
                  "Enter the exact candidate UUID from its owned run or lineage.",
                ),
              );
              return;
            }
            chooseSource({ kind: "candidate", id: candidateId.trim() });
          }}
        >
          <label className="workbench-field">
            {title} candidate ID
            <input
              value={candidateId}
              onChange={(event) => setCandidateId(event.target.value)}
            />
          </label>
          <Button type="submit" variant="outline">
            Load {title} source
          </Button>
          <p className="text-xs muted mt-2">
            Copy the exact candidate ID from an owned run or version tree.
            Ownership and source association are checked before leaf selection;
            arbitrary code is never accepted here.
          </p>
        </form>
      )}
      {(problem || specProblem || leafProblem) && (
        <ErrorState error={new Error(problem || specProblem || leafProblem!)} />
      )}
      {inputError != null && <ErrorState error={inputError} />}
      {(strategy.error || candidate.error || spec.error || leaf.error) && (
        <ErrorState
          error={strategy.error || candidate.error || spec.error || leaf.error}
        />
      )}
      {source.id && spec.isPending && (
        <Loading text={`Verifying ${title} exact source…`} />
      )}
      {spec.data && hash && !problem && !specProblem && (
        <>
          <Metadata
            values={{
              [`${title} source`]: `${spec.data.resource_kind} · ${spec.data.resource_id}`,
              "Source strategy version": spec.data.strategy_version_id,
              "Code SHA-256": spec.data.code_sha256,
              "Description revision": spec.data.revision,
              "Description SHA-256": spec.data.spec_sha256,
            }}
          />
          <label className="workbench-field">
            {title} actual leaf Evaluation
            <select
              value={pins.evaluation_id || ""}
              onChange={(event) =>
                chooseLeaf(
                  allLeaves.find(
                    (item) => item.evaluation_id === event.target.value,
                  ),
                )
              }
            >
              <option value="">Choose a physical leaf explicitly</option>
              {allLeaves.map((item) => (
                <option key={item.evaluation_id} value={item.evaluation_id}>
                  {item.split} · {item.status} · {item.evaluation_id}
                </option>
              ))}
            </select>
          </label>
          {leaves.data && (
            <Pagination
              offset={leafOffset}
              limit={25}
              count={leaves.data.count}
              busy={leaves.isFetching}
              onPage={setLeafOffset}
            />
          )}
          {leaves.error && <ErrorState error={leaves.error} />}
          {leaves.data?.count === 0 && (
            <p className="notice">
              This source has no owned physical leaf. No aggregate, same-code
              candidate or current source was substituted.
            </p>
          )}
          {leaf.data && !leafProblem && (
            <>
              <Metadata
                values={{
                  Status: leaf.data.status,
                  Window: leaf.data.window,
                  Dataset: leaf.data.dataset_version_id,
                  Universe: leaf.data.universe_version_id,
                  "Execution attempt": pins.execution_attempt_id,
                  "Result manifest SHA-256": pins.result_manifest_sha256,
                  "Trace manifest SHA-256":
                    pins.trace_manifest_sha256 ??
                    "Historical absence (not an empty trace)",
                }}
              />
              {leaf.data.missing_reasons.map((reason) => (
                <p className="notice" key={reason}>
                  {reason}
                </p>
              ))}
              {leaf.data.status !== "succeeded" && (
                <p className="notice">
                  This leaf did not succeed. It cannot be represented as a
                  zero-result successful comparison.
                </p>
              )}
            </>
          )}
        </>
      )}
    </fieldset>
  );
}
