import { useCallback, useMemo } from "react";
import {
  Background,
  Controls,
  ReactFlow,
  type NodeChange,
  type Node,
  type Edge,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { StrategySpec, DecisionDetail } from "@/client/types.gen";
import { ruleObservation } from "@/lib/workbench-evidence";

export function RuleGraph({
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
  const nodes = useMemo<Node[]>(
    () =>
      spec.rules.map((rule, index) => ({
        id: rule.rule_id,
        position: { x: (index % 3) * 260, y: Math.floor(index / 3) * 150 },
        data: {
          label: (
            <>
              <strong>{rule.title}</strong>
              <div style={{ fontSize: 11, marginTop: 5 }}>
                Expected {rule.category}
              </div>
              <div style={{ fontSize: 10, marginTop: 5 }}>
                {ruleObservation(rule, detail).status}
              </div>
            </>
          ),
        },
        selected: selectedRule === rule.rule_id,
        draggable: false,
        connectable: false,
        deletable: false,
        focusable: true,
        className: "nopan",
        ariaLabel: `Expected rule: ${rule.title}`,
        style: {
          width: 225,
          borderColor: selectedRule === rule.rule_id ? "#267c68" : undefined,
        },
      })),
    [spec, selectedRule, detail],
  );
  const edges = useMemo<Edge[]>(
    () =>
      spec.edges.map(([source, target], index) => ({
        id: `${source}-${target}-${index}`,
        source,
        target,
        selectable: false,
        focusable: false,
        deletable: false,
        reconnectable: false,
        animated: false,
      })),
    [spec],
  );
  const click = useCallback(
    (_event: unknown, node: Node) => onSelect(node.id),
    [onSelect],
  );
  const changes = useCallback(
    (values: NodeChange[]) => {
      const selected = values.find(
        (value) => value.type === "select" && value.selected,
      );
      if (selected?.type === "select") onSelect(selected.id);
    },
    [onSelect],
  );
  return (
    <section aria-label="Expected template rule graph">
      <p className="notice">
        Expected template flow, not an execution trace. Edges do not prove
        branches ran. Only a selected recorded stage below supplies actual
        values. This graph cannot edit or execute a strategy.
      </p>
      <div className="workbench-graph">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodeClick={click}
          onNodesChange={changes}
          nodesDraggable={false}
          nodesConnectable={false}
          edgesReconnectable={false}
          nodesFocusable
          edgesFocusable={false}
          deleteKeyCode={null}
          multiSelectionKeyCode={null}
          selectionKeyCode={null}
          fitView
          minZoom={0.25}
          maxZoom={1.5}
          preventScrolling={false}
        >
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <p className="text-xs muted">
        Keyboard: Tab to a rule and Enter to inspect it. The rule-card buttons
        provide the same selection outside the graph.
      </p>
    </section>
  );
}
