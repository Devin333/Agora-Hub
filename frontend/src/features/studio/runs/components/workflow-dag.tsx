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

export function WorkflowDag({ nodes, edges }: { nodes: WorkflowDagNode[]; edges: WorkflowDagEdge[] }) {
  const selectStep = useRunInspectorStore((state) => state.selectStep)
  const setActiveTab = useRunInspectorStore((state) => state.setActiveTab)
  const selectedNodeId = useRunInspectorStore((state) => state.selectedNodeId)

  const flowNodes = useMemo(
    () =>
      layoutWorkflowNodes(nodes).map((node) => ({
        ...node,
        selected: node.id === selectedNodeId,
        className: node.id === selectedNodeId ? "ring-2 ring-primary rounded-md" : undefined
      })),
    [nodes, selectedNodeId]
  )

  const flowEdges: Edge[] = useMemo(
    () =>
      edges.map((edge) => ({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        label: edge.label,
        animated: nodes.find((node) => node.id === edge.target)?.status === "running",
        style: { stroke: "var(--border)" }
      })),
    [edges, nodes]
  )

  return (
    <div className="h-[520px] overflow-hidden rounded-lg border border-border bg-card">
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
        <Controls />
        <MiniMap pannable zoomable nodeStrokeWidth={2} />
      </ReactFlow>
    </div>
  )
}
