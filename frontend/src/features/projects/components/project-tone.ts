import type { BadgeVariant } from "@/components/ui/badge"

/**
 * Single mapping from Projects domain states (priority, severity, workflow tone,
 * data state, Lab graph node type) to theme tokens, so no component hard-codes hues.
 */

export type ProjectTone = "success" | "warning" | "danger" | "info" | "neutral"

const toneSurface: Record<ProjectTone, string> = {
  success: "bg-success/15 text-success-text",
  warning: "bg-warning/15 text-warning-text",
  danger: "bg-destructive/10 text-destructive-text",
  info: "bg-link/15 text-link",
  neutral: "bg-muted text-muted-foreground",
}

const toneText: Record<ProjectTone, string> = {
  success: "text-success-text",
  warning: "text-warning-text",
  danger: "text-destructive-text",
  info: "text-link",
  neutral: "text-muted-foreground",
}

const toneDot: Record<ProjectTone, string> = {
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-destructive",
  info: "bg-link",
  neutral: "bg-faint-foreground",
}

/** Tinted surface (background + text) for a tone. */
export function toneSurfaceClass(tone: ProjectTone): string {
  return toneSurface[tone]
}

export function toneTextClass(tone: ProjectTone): string {
  return toneText[tone]
}

export function toneDotClass(tone: ProjectTone): string {
  return toneDot[tone]
}

/** Watchlist priority and signal severity share the same high/medium/low scale. */
export function levelTone(level: unknown): ProjectTone {
  if (level === "high") return "danger"
  if (level === "medium") return "warning"
  return "neutral"
}

export function workflowTone(statusTone: string | undefined): ProjectTone {
  if (statusTone === "success") return "success"
  if (statusTone === "warning") return "warning"
  return "neutral"
}

export function dataStateBadgeVariant(dataState: string): BadgeVariant {
  if (dataState === "ready") return "success"
  if (dataState === "empty") return "muted"
  return "warning"
}

/** Lab graph node fills resolve to CSS custom properties so the SVG follows the theme. */
const labNodeColors: Record<string, string> = {
  user_problem: "var(--foreground)",
  case: "var(--link)",
  solution: "var(--success)",
  question: "var(--warning)",
  feedback: "var(--status-working)",
  pattern: "var(--link)",
  component: "var(--destructive)",
}

export function labNodeColor(nodeType: string): string {
  return labNodeColors[nodeType] ?? "var(--muted-foreground)"
}

export const labGraphEdgeColor = "color-mix(in srgb, var(--foreground) 25%, transparent)"
