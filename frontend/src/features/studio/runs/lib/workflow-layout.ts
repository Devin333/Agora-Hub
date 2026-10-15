import type { WorkflowDagNode } from "@/types/agent"

const typeOrder: Record<string, number> = {
  collect: 0,
  process: 1,
  rank: 2,
  memory: 3,
  research: 4,
  report: 5,
  quality: 6,
  final: 7,
  custom: 8
}

export function layoutWorkflowNodes(nodes: WorkflowDagNode[]) {
  return nodes
    .slice()
    .sort((a, b) => (typeOrder[a.type] ?? 99) - (typeOrder[b.type] ?? 99))
    .map((node, index) => ({
      id: node.id,
      type: "workflowNode",
      position: {
        x: index * 240,
        y: index % 2 === 0 ? 40 : 170
      },
      data: node
    }))
}
