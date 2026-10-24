import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { LineageNode, WorkbenchLineage } from "@/client/types.gen";
import { ErrorState, Loading, Panel } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Pagination } from "@/components/data-common";
import {
  workbenchLineageOptions,
  setCandidateBookmark,
  type SourceResource,
} from "@/lib/workbench-api";
import { queryClient } from "@/lib/api";
export type OpenSource = (source: SourceResource, strategyId: string) => void;
function NodeSummary({
  node,
  selected,
  onOpen,
}: {
  node: LineageNode;
  selected?: boolean;
  onOpen: OpenSource;
}) {
  return (
    <div className="workbench-lineage-node" data-selected={selected}>
      <button
        type="button"
        className="text-link text-left break-all"
        aria-label={`Open version: ${node.name}`}
        onClick={() =>
          onOpen(
            { kind: node.resource_kind, id: node.id },
            node.strategy_version_id,
          )
        }
      >
        {node.name}
      </button>
      <div className="flex flex-wrap gap-1 mt-2">
        <Badge variant="outline">{node.kind}</Badge>
        {node.bookmarked && <Badge variant="secondary">Bookmarked</Badge>}
        {selected && <Badge>Selected</Badge>}
        {node.is_recorded_best === true && (
          <Badge variant="secondary">Recorded best</Badge>
        )}
        {node.final_selection_count > 0 && (
          <Badge variant="outline">
            Frozen selection ({node.final_selection_count})
          </Badge>
        )}
      </div>
      <p className="text-xs mt-2 break-all">{node.id}</p>
      <p className="text-xs muted break-all">Code: {node.code_sha256}</p>
      {node.selection_missing_reason && (
        <p className="text-xs muted mt-2">{node.selection_missing_reason}</p>
      )}
      {node.aggregate_missing_reason && (
        <p className="text-xs muted mt-2">{node.aggregate_missing_reason}</p>
      )}
      {node.final_selection_ids?.length > 0 && (
        <details className="text-xs mt-2">
          <summary>Recorded final selection references</summary>
          {node.final_selection_ids.map((id) => (
            <p className="break-all" key={id}>
              {id}
            </p>
          ))}
          {node.final_selections_truncated && (
            <p>Only the bounded first selection references are shown.</p>
          )}
        </details>
      )}
      {node.evaluation_count === 0 ? (
        <p className="text-xs muted mt-2">
          No leaf evaluation recorded. No performance inferred.
        </p>
      ) : (
        <div className="text-xs mt-2">
          {node.evaluations.map((item) => (
            <p key={item.evaluation_id}>
              {item.split}: {item.status} · {item.evaluation_id}
              {item.error ? ` · ${item.error}` : ""}
            </p>
          ))}
          {node.evaluations_truncated && (
            <p>
              Showing {node.evaluations.length} of {node.evaluation_count}{" "}
              leaves. Open this version for paginated evaluation selection.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
function Branch({
  node,
  ancestors,
  onOpen,
}: {
  node: LineageNode;
  ancestors: string[];
  onOpen: OpenSource;
}) {
  const [expanded, setExpanded] = useState(false);
  const [offset, setOffset] = useState(0);
  const cycle = ancestors.includes(`${node.resource_kind}:${node.id}`);
  const children = useQuery({
    ...workbenchLineageOptions(
      { kind: node.resource_kind, id: node.id },
      offset,
    ),
    enabled: expanded && !cycle,
  });
  return (
    <>
      <NodeSummary node={node} onOpen={onOpen} />
      {node.child_count > 0 && !cycle && (
        <details onToggle={(event) => setExpanded(event.currentTarget.open)}>
          <summary className="text-xs text-link">
            Descendants of {node.name}
          </summary>
          {expanded && (
            <div className="workbench-lineage-children">
              {children.isPending ? (
                <Loading text="Loading descendants…" />
              ) : children.error ? (
                <ErrorState error={children.error} />
              ) : (
                children.data && (
                  <>
                    {children.data.children.map((child) => (
                      <Branch
                        key={`${child.resource_kind}:${child.id}`}
                        node={child}
                        ancestors={[
                          ...ancestors,
                          `${node.resource_kind}:${node.id}`,
                        ]}
                        onOpen={onOpen}
                      />
                    ))}
                    <Pagination
                      offset={offset}
                      limit={25}
                      count={children.data.child_count}
                      busy={children.isFetching}
                      onPage={setOffset}
                    />
                  </>
                )
              )}
            </div>
          )}
        </details>
      )}
      {cycle && (
        <p className="notice">
          Repeated lineage reference shown without recursive expansion.
        </p>
      )}
    </>
  );
}
function Bookmark({ node }: { node: LineageNode }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState<boolean | null>(null);
  const [notice, setNotice] = useState("");
  async function save() {
    if (busy) return;
    const desired = pending ?? !node.bookmarked;
    setPending(desired);
    setBusy(true);
    setError(null);
    try {
      const result = await setCandidateBookmark(node.id, desired);
      if (result.code_sha256 !== node.code_sha256)
        throw new Error(
          "Bookmark response source differs from this candidate.",
        );
      setNotice(
        result.bookmarked
          ? "Candidate bookmark saved."
          : "Candidate bookmark removed.",
      );
      setPending(null);
      await queryClient.invalidateQueries({ queryKey: ["workbench-lineage"] });
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Button
        variant="outline"
        size="sm"
        disabled={busy}
        onClick={() => void save()}
      >
        {busy
          ? "Saving bookmark…"
          : pending !== null
            ? "Retry same bookmark change"
            : node.bookmarked
              ? "Remove candidate bookmark"
              : "Bookmark candidate"}
      </Button>
      {error != null && <ErrorState error={error} />}
      {notice && (
        <p role="status" className="text-xs mt-2">
          {notice}
        </p>
      )}
    </>
  );
}
export function LineageSidebar({
  lineage,
  offset,
  busy,
  onPage,
  onOpen,
  onCompareParent,
}: {
  lineage: WorkbenchLineage;
  offset: number;
  busy: boolean;
  onPage: (offset: number) => void;
  onOpen: OpenSource;
  onCompareParent?: () => void;
}) {
  const focus = lineage.focus;
  return (
    <aside aria-label="Version lineage">
      <Panel title="Version lineage">
        <p className="text-xs muted mb-3">
          Code parents form the main tree. Failed and repair versions retain
          their own source and results. Algorithm references are separate.
        </p>
        {lineage.seed_strategy && (
          <details>
            <summary>Run seed strategy (not candidate parent)</summary>
            <NodeSummary node={lineage.seed_strategy} onOpen={onOpen} />
          </details>
        )}
        {lineage.parent && (
          <section aria-label="Code parent">
            <h3 className="text-sm font-semibold">Code parent</h3>
            <NodeSummary node={lineage.parent} onOpen={onOpen} />
            {onCompareParent && (
              <Button variant="outline" size="sm" onClick={onCompareParent}>
                Compare with code parent
              </Button>
            )}
          </section>
        )}
        <NodeSummary node={focus} selected onOpen={onOpen} />
        {focus.resource_kind === "candidate" && (
          <Bookmark key={focus.id} node={focus} />
        )}
        <details open>
          <summary className="mt-4">Descendants of {focus.name}</summary>
          <div className="workbench-lineage-children">
            {lineage.children.map((child) => (
              <Branch
                key={`${child.resource_kind}:${child.id}`}
                node={child}
                ancestors={[`${focus.resource_kind}:${focus.id}`]}
                onOpen={onOpen}
              />
            ))}
            {lineage.child_count === 0 && (
              <p className="text-xs muted mt-2">No recorded descendants.</p>
            )}
            <Pagination
              offset={offset}
              limit={25}
              count={lineage.child_count}
              busy={busy}
              onPage={onPage}
            />
          </div>
        </details>
        <section className="mt-5">
          <h3 className="text-sm font-semibold">
            Algorithm references (not code parents)
          </h3>
          {lineage.reference_candidates.length ? (
            lineage.reference_candidates.map((reference) => (
              <div key={reference.candidate_id} className="mt-2 text-xs">
                <button
                  type="button"
                  className="text-link"
                  onClick={() =>
                    onOpen(
                      { kind: "candidate", id: reference.candidate_id },
                      focus.strategy_version_id,
                    )
                  }
                >
                  {reference.candidate_key}
                </button>
                <p className="break-all">{reference.code_sha256}</p>
                <p>{reference.relationship}</p>
              </div>
            ))
          ) : (
            <p className="text-xs muted mt-2">
              No algorithm references recorded.
            </p>
          )}
        </section>
        <details className="mt-4">
          <summary>Original iteration context</summary>
          <p className="text-xs">{lineage.context.status}</p>
          <p className="text-xs">
            Generation outcome:{" "}
            {lineage.context.generation_status || "Not recorded"}
          </p>
          <p className="text-sm whitespace-pre-wrap">
            {lineage.context.feedback || "No original feedback recorded."}
          </p>
          {lineage.context.feedback_truncated && (
            <p className="notice">
              Original feedback exceeds this bounded view and is explicitly
              truncated.
            </p>
          )}
          {lineage.context.feedback_sha256 && (
            <p className="text-xs break-all">
              Original feedback hash: {lineage.context.feedback_sha256}
            </p>
          )}
          <p className="text-xs">
            Revision mode: {lineage.context.revision_mode || "Not recorded"}
          </p>
          {lineage.context.model_call_ids?.map((id) => (
            <p key={id} className="text-xs break-all">
              Model call: {id}
            </p>
          ))}
          {lineage.context.missing_reasons?.map((reason) => (
            <p key={reason} className="text-xs muted">
              {reason}
            </p>
          ))}
        </details>
        {lineage.missing_reasons.map((reason) => (
          <p key={reason} className="notice mt-3">
            {reason}
          </p>
        ))}
      </Panel>
    </aside>
  );
}
