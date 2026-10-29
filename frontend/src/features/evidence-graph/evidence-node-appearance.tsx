import { BookOpen, Building2, Check, Cpu, FileText, GitBranch, MessageSquareText, Newspaper, type LucideIcon } from "lucide-react"
import { cn } from "@/lib/utils"
import type { EvidenceNodeType } from "@/types/evidence-graph"

export const nodeTypeLabels: Record<EvidenceNodeType, string> = {
  topic: "Topic",
  paper: "Paper",
  project: "Project",
  news: "News",
  community_signal: "Community",
  company: "Company",
  model: "Model",
  method: "Method",
  task: "Task",
  report: "Report",
}

type NodeKindAppearance = {
  icon: LucideIcon
  /** Foreground hue for the node kind (token-based). */
  text: string
  /** Tinted surface for the node kind marker (token-based). */
  tint: string
}

/**
 * Node-kind hues map onto the rakazo status tokens so the evidence graph stays
 * legible in both themes without any hard-coded palette colors.
 */
const nodeKindAppearance: Record<EvidenceNodeType, NodeKindAppearance> = {
  topic: { icon: Check, text: "text-foreground", tint: "bg-foreground/10" },
  paper: { icon: BookOpen, text: "text-link", tint: "bg-link/15" },
  method: { icon: BookOpen, text: "text-link", tint: "bg-link/15" },
  task: { icon: BookOpen, text: "text-link", tint: "bg-link/15" },
  project: { icon: GitBranch, text: "text-success", tint: "bg-success/15" },
  news: { icon: Newspaper, text: "text-warning", tint: "bg-warning/15" },
  community_signal: { icon: MessageSquareText, text: "text-status-working", tint: "bg-status-working/15" },
  company: { icon: Building2, text: "text-destructive", tint: "bg-destructive/15" },
  model: { icon: Cpu, text: "text-status-working", tint: "bg-status-working/15" },
  report: { icon: FileText, text: "text-muted-foreground", tint: "bg-muted-foreground/15" },
}

export function nodeKindAppearanceFor(type: EvidenceNodeType): NodeKindAppearance {
  return nodeKindAppearance[type] ?? nodeKindAppearance.topic
}

/** Bare kind icon tinted with the node-kind hue. */
export function NodeKindIcon({ type, className }: { type: EvidenceNodeType; className?: string }) {
  const { icon: Icon, text } = nodeKindAppearanceFor(type)
  return <Icon size={15} strokeWidth={1.8} aria-hidden="true" className={cn("shrink-0", text, className)} />
}

/** Rounded kind marker: tinted square with the kind icon, used at the start of list rows. */
export function NodeKindMarker({ type, className }: { type: EvidenceNodeType; className?: string }) {
  const { icon: Icon, text, tint } = nodeKindAppearanceFor(type)
  return (
    <span aria-hidden="true" className={cn("flex size-7 shrink-0 items-center justify-center rounded-lg", tint, text, className)}>
      <Icon size={15} strokeWidth={1.8} />
    </span>
  )
}

/** Inline pill naming the node kind, hue-tinted. */
export function NodeKindPill({ type, className }: { type: EvidenceNodeType; className?: string }) {
  const { icon: Icon, text, tint } = nodeKindAppearanceFor(type)
  return (
    <span className={cn("inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full px-2.5 text-label font-medium", tint, text, className)}>
      <Icon size={14} strokeWidth={1.8} aria-hidden="true" />
      {nodeTypeLabels[type]}
    </span>
  )
}
