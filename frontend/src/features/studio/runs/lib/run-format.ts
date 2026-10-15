import type { AgentRunStatus, StepStatus } from "@/types/agent"

export function formatDuration(ms?: number): string {
  if (ms === undefined) return "N/A"
  const seconds = Math.max(0, Math.round(ms / 1000))
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  if (minutes < 1) return `${remainder}s`
  if (minutes < 60) return `${minutes}m ${remainder}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

export function formatBytes(value?: number): string {
  if (value === undefined) return "N/A"
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

export function normalizeRunStatus(status: string): AgentRunStatus {
  if (status === "queued") return "pending"
  if (status === "completed" || status === "succeeded") return "success"
  if (status === "review_required" || status === "human_review_required") return "waiting_for_human"
  return status as AgentRunStatus
}

export function statusTone(status: AgentRunStatus | StepStatus): "neutral" | "success" | "warning" | "danger" | "info" | "accent" {
  if (status === "success" || status === "succeeded") return "success"
  if (status === "running") return "info"
  if (status === "failed" || status === "cancelled") return "danger"
  if (status === "partially_failed" || status === "blocked" || status === "waiting_for_human" || status === "skipped") return "warning"
  return "neutral"
}

export function formatRunStatus(status: string): string {
  const labels: Record<string, string> = {
    pending: "Pending",
    running: "Running",
    success: "Success",
    succeeded: "Success",
    failed: "Failed",
    partially_failed: "Partially failed",
    cancelled: "Cancelled",
    blocked: "Blocked",
    waiting_for_human: "Waiting for human",
    skipped: "Skipped",
    warning: "Warning",
    passed: "Passed"
  }
  return labels[status] ?? status
}

export function shortRunId(id: string): string {
  if (id.length <= 18) return id
  return `${id.slice(0, 8)}...${id.slice(-6)}`
}
