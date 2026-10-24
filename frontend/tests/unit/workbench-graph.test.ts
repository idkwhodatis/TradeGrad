import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { expect, it, vi } from "vitest";
import type { ReactFlowProps } from "@xyflow/react";
import { spec } from "./workbench-fixtures";
let props: ReactFlowProps;
let controls: { showInteractive?: boolean };
vi.mock("@xyflow/react", () => ({
  ReactFlow: (value: ReactFlowProps) => {
    props = value;
    return createElement(
      "div",
      { "data-testid": "flow-contract" },
      value.children,
    );
  },
  Controls: (value: { showInteractive?: boolean }) => {
    controls = value;
    return null;
  },
  Background: () => null,
}));
it("locks every mutation control while retaining accessible rule selection", async () => {
  const { RuleGraph } =
    await import("../../src/features/research-workbench/rule-graph");
  const select = vi.fn();
  const html = renderToString(
    createElement(RuleGraph, {
      spec: spec.spec,
      selectedRule: "filter",
      onSelect: select,
    }),
  );
  expect(html).toContain("not an execution trace");
  expect(props.nodesDraggable).toBe(false);
  expect(props.nodesConnectable).toBe(false);
  expect(props.edgesReconnectable).toBe(false);
  expect(props.deleteKeyCode).toBeNull();
  expect(props.nodesFocusable).toBe(true);
  expect(controls.showInteractive).toBe(false);
  for (const node of props.nodes!) {
    expect(node.draggable).toBe(false);
    expect(node.connectable).toBe(false);
    expect(node.deletable).toBe(false);
    expect(node.focusable).toBe(true);
    expect(node.ariaLabel).toContain("Expected rule:");
  }
  for (const edge of props.edges!) {
    expect(edge.reconnectable).toBe(false);
    expect(edge.deletable).toBe(false);
  }
  props.onNodesChange!([{ type: "select", id: "rank", selected: true }]);
  expect(select).toHaveBeenCalledExactlyOnceWith("rank");
  props.onNodesChange!([
    { type: "position", id: "rank", position: { x: 999, y: 999 } },
    { type: "remove", id: "filter" },
  ]);
  expect(select).toHaveBeenCalledTimes(1);
  expect(props.nodes![1].position).toEqual({ x: 260, y: 0 });
});
