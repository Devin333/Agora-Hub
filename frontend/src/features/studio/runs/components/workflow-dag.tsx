"use client"

import { useMemo } from "react"
import ReactFlow, { Background, Controls, MiniMap, type Edge, type NodeTypes } from "reactflow"
import { WorkflowNode } from "@/features/studio/runs/components/workflow-node"
import { layoutWorkflowNodes } from "@/features/studio/runs/lib/workflow-layout"
import { useRunInspectorStore } from "@/stores/run-inspector-store"
import type { WorkflowDagEdge, WorkflowDagNode } from "@/types/agent"

const nodeTypes: NodeTypes = {
  workflowNode: WorkflowNode
}

/**
 * React Flow ships light-only chrome; these overrides re-skin controls, minimap and
 * attribution with theme tokens so the canvas follows the active appearance.
 */
const flowSurfaceClass = [
  "h-[520px] overflow-hidden rounded-2xl border border-border bg-card",
  "[&_.react-flow__controls]:overflow-hidden [&_.react-flow__controls]:rounded-lg [&_.react-flow__controls]:border [&_.react-flow__controls]:border-border [&_.react-flow__controls]:shadow-none",
  "[&_.react-flow__controls-button]:border-border [&_.react-flow__controls-button]:bg-card [&_.react-flow__controls-button]:fill-foreground [&_.react-flow__controls-button:hover]:bg-accent",
  "[&_.react-flow__minimap]:overflow-hidden [&_.react-flow__minimap]:rounded-lg [&_.react-flow__minimap]:border [&_.react-flow__minimap]:border-border",
  "[&_.react-flow__minimap-mask]:fill-background/60",
  "[&_.react-flow__attribution]:bg-transparent [&_.react-flow__attribution_a]:text-faint-foreground"
].join(" ")

export function WorkflowDag({ nodes, edges }: { nodes: WorkflowDagNode[]; edges: WorkflowDagEdge[] }) {
  const selectStep = useRunInspectorStore((state) => state.selectStep)
  const setActiveTab = useRunInspectorStore((state) => state.setActiveTab)
  const selectedNodeId = useRunInspectorStore((state) => state.selectedNodeId)

  const flowNodes = useMemo(
    () =>
      layoutWorkflowNodes(nodes).map((node) => ({
        ...node,
        selected: node.id === selectedNodeId,
        className: node.id === selectedNodeId ? "rounded-xl ring-2 ring-ring" : undefined
      })),
    [nodes, selectedNodeId]
  )

  const flowEdges: Edge[] = useMemo(
    () =>
      edges.map((edge) => {
        const running = nodes.find((node) => node.id === edge.target)?.status === "running"
        return {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          label: edge.label,
          animated: running,
          style: { stroke: running ? "var(--status-working)" : "var(--border)", strokeWidth: 1.5 },
          labelStyle: { fill: "var(--muted-foreground)", fontSize: 11 },
          labelBgStyle: { fill: "var(--card)" }
        }
      }),
    [edges, nodes]
  )

  return (
    <div className={flowSurfaceClass}>
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        fitView
        minZoom={0.35}
        onNodeClick={(_, node) => {
          const data = node.data as WorkflowDagNode
          selectStep(data.id, data.stepId)
          if (data.status === "failed") setActiveTab("errors")
        }}
      >
        <Background color="var(--border)" gap={24} />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable nodeStrokeWidth={2} nodeClassName="fill-muted" style={{ backgroundColor: "var(--card)" }} />
      </ReactFlow>
    </div>
  )
}
