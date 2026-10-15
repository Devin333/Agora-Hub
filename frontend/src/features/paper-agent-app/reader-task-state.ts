import type { ReaderTaskActivity } from "@/features/portal/reader/personal-memory-task-context"
import type { ResearchPersonalMemoryRef } from "@/lib/research/workspace-items"

export type ReaderTaskSnapshot = Pick<ReaderTaskActivity, "kind" | "requestId" | "generation" | "status">

export function readerTaskSnapshots(activities: Iterable<ReaderTaskActivity>): ReaderTaskSnapshot[] {
  const snapshots = new Map<string, ReaderTaskSnapshot>()
  for (const activity of activities) {
    snapshots.set(`${activity.kind}:${activity.requestId}`, {
      kind: activity.kind,
      requestId: activity.requestId,
      generation: activity.generation,
      status: activity.status,
    })
  }
  return [...snapshots.values()]
}

export function activeReaderRequestIds(activities: Iterable<ReaderTaskActivity>): string[] {
  return [...new Set([...activities]
    .filter(activity => activity.status === "waiting" || activity.status === "running" || activity.status === "unknown")
    .map(activity => activity.requestId))]
}

export function usedReaderMemoryRefs(activities: Iterable<ReaderTaskActivity>): ResearchPersonalMemoryRef[] {
  const refs = new Map<string, ResearchPersonalMemoryRef>()
  for (const activity of activities) {
    for (const ref of activity.usedPersonalMemory) refs.set(`${ref.memoryId}:${ref.revision}`, ref)
  }
  return [...refs.values()]
}
