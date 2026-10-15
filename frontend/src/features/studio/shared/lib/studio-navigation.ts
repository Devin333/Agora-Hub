import {
  Archive,
  BarChart3,
  ClipboardCheck,
  Database,
  Gauge,
  Network,
  ShieldCheck,
  UserCheck,
  Workflow
} from "lucide-react"
import { formatStatus, translate } from "@/lib/i18n"
import type { LocaleMode } from "@/stores/ui-store"
import type { StudioModuleEntry, StudioNavigationGroup } from "@/types/studio"

export const studioNavigationGroups: StudioNavigationGroup[] = [
  {
    label: "Runtime",
    items: [
      {
        label: "Run Center",
        href: "/studio/runs",
        description: "Inspect agent run history, traces, events, and details.",
        icon: Workflow,
        status: "partial"
      },
      {
        label: "Artifact / Replay",
        href: "/studio/artifacts",
        description: "Browse generated artifacts and replay material.",
        icon: Archive,
        status: "partial"
      }
    ]
  },
  {
    label: "Business",
    items: [
      {
        label: "Board Center",
        href: "/studio/boards",
        description: "Inspect AI News, Project Radar, Paper Radar, Community Pulse, and Cross Board.",
        icon: BarChart3,
        status: "partial"
      },
      {
        label: "Evidence Center",
        href: "/studio/evidence",
        description: "Audit claim support, citation failures, and evidence lineage.",
        icon: Network,
        status: "partial"
      }
    ]
  },
  {
    label: "Governance",
    items: [
      {
        label: "Quality Gate",
        href: "/studio/quality",
        description: "Review quality results before downstream publishing.",
        icon: ShieldCheck,
        status: "partial"
      },
      {
        label: "Human Review",
        href: "/studio/review",
        description: "Approve, reject, modify, and resume human-gated work.",
        icon: UserCheck,
        status: "partial"
      }
    ]
  },
  {
    label: "System",
    items: [
      {
        label: "Sources",
        href: "/studio/sources",
        description: "Monitor configured source health and fetch previews.",
        icon: Database,
        status: "ready"
      }
    ]
  }
]

export const studioModuleEntries: StudioModuleEntry[] = [
  {
    title: "Run Center",
    description: "Inspect workflow runs, statuses, step traces, logs, and errors.",
    href: "/studio/runs",
    coreObject: "Agent run",
    targetApi: "/api/v1/runs",
    status: "partial",
    icon: Workflow,
    actionLabel: "Open runs"
  },
  {
    title: "Board Center",
    description: "Monitor business board outputs and cross-board intelligence signals.",
    href: "/studio/boards",
    coreObject: "Board",
    targetApi: "/api/v1/boards",
    status: "partial",
    icon: BarChart3,
    actionLabel: "Open boards"
  },
  {
    title: "Evidence Center",
    description: "Audit claim support, unsupported sections, citation failures, and lineage.",
    href: "/studio/evidence",
    coreObject: "Evidence",
    targetApi: "/api/v1/runs/{run_id}/lineage",
    status: "partial",
    icon: Network,
    actionLabel: "Open evidence"
  },
  {
    title: "Artifact / Replay",
    description: "Explore runtime outputs, generated files, and replay-ready evidence.",
    href: "/studio/artifacts",
    coreObject: "Artifact",
    targetApi: "/api/v1/runs/{run_id}/artifacts",
    status: "partial",
    icon: Archive,
    actionLabel: "Open artifacts"
  },
  {
    title: "Quality Gate",
    description: "Track checks that decide whether a report is ready to publish.",
    href: "/studio/quality",
    coreObject: "Quality result",
    targetApi: "/api/v1/quality",
    status: "partial",
    icon: ShieldCheck,
    actionLabel: "Open quality"
  },
  {
    title: "Human Review",
    description: "Review high-risk operations, blocked runs, approvals, and decision history.",
    href: "/studio/review",
    coreObject: "Approval",
    targetApi: "/api/v1/approvals",
    status: "partial",
    icon: ClipboardCheck,
    actionLabel: "Open review"
  },
  {
    title: "Sources",
    description: "Review source configuration, health, and fetch previews.",
    href: "/studio/sources",
    coreObject: "Source",
    targetApi: "/api/v1/sources",
    status: "ready",
    icon: Database,
    actionLabel: "Open sources"
  }
]

export function getStudioNavigationItems() {
  return studioNavigationGroups.flatMap((group) => group.items)
}

export function getLocalizedStudioNavigationGroups(locale: LocaleMode): StudioNavigationGroup[] {
  return studioNavigationGroups.map((group) => ({
    ...group,
    label: translate(locale, studioGroupLabelKeys[group.label] ?? group.label),
    items: group.items.map((item) => {
      const copy = studioEntryCopyByHref[item.href]
      return copy
        ? {
            ...item,
            label: translate(locale, copy.title),
            description: translate(locale, copy.description)
          }
        : item
    })
  }))
}

export function getLocalizedStudioModuleEntries(locale: LocaleMode): StudioModuleEntry[] {
  return studioModuleEntries.map((entry) => {
    const copy = studioEntryCopyByHref[entry.href]
    return copy
      ? {
          ...entry,
          title: translate(locale, copy.title),
          description: translate(locale, copy.description),
          coreObject: translate(locale, copy.coreObject),
          actionLabel: translate(locale, copy.action)
        }
      : entry
  })
}

export function getStudioBreadcrumb(pathname: string, locale: LocaleMode = "en"): string[] {
  const items = getLocalizedStudioNavigationGroups(locale).flatMap((group) => group.items)
  const exactMatch = items.find((item) => item.href === pathname)
  if (exactMatch) return [translate(locale, "studio.title"), exactMatch.label]

  const parentMatch = items
    .filter((item) => pathname.startsWith(`${item.href}/`))
    .sort((a, b) => b.href.length - a.href.length)[0]

  return [translate(locale, "studio.title"), parentMatch?.label ?? translate(locale, "studio.module.runCenter.title")]
}

export function studioStatusLabel(status: StudioModuleEntry["status"], locale: LocaleMode = "en") {
  return formatStatus(locale, status)
}

export function studioStatusTone(status: StudioModuleEntry["status"]) {
  if (status === "ready") return "success"
  if (status === "partial") return "warning"
  return "muted"
}

export const studioEnvironment = {
  label: "Operations Studio",
  status: "API first, fallback visible",
  icon: Gauge
}

export function getStudioEnvironment(locale: LocaleMode) {
  return {
    ...studioEnvironment,
    label: translate(locale, "studio.environment.label"),
    status: translate(locale, "studio.environment.status")
  }
}

const studioGroupLabelKeys: Record<string, Parameters<typeof translate>[1]> = {
  Runtime: "studio.nav.runtime",
  Business: "studio.nav.business",
  Governance: "studio.nav.governance",
  System: "studio.nav.system"
}

const studioEntryCopyByHref: Record<string, {
  title: Parameters<typeof translate>[1]
  description: Parameters<typeof translate>[1]
  coreObject: Parameters<typeof translate>[1]
  action: Parameters<typeof translate>[1]
}> = {
  "/studio/runs": {
    title: "studio.module.runCenter.title",
    description: "studio.module.runCenter.description",
    coreObject: "studio.module.runCenter.coreObject",
    action: "studio.module.runCenter.action"
  },
  "/studio/boards": {
    title: "studio.module.boardCenter.title",
    description: "studio.module.boardCenter.description",
    coreObject: "studio.module.boardCenter.coreObject",
    action: "studio.module.boardCenter.action"
  },
  "/studio/evidence": {
    title: "studio.module.evidenceCenter.title",
    description: "studio.module.evidenceCenter.description",
    coreObject: "studio.module.evidenceCenter.coreObject",
    action: "studio.module.evidenceCenter.action"
  },
  "/studio/artifacts": {
    title: "studio.module.artifactReplay.title",
    description: "studio.module.artifactReplay.description",
    coreObject: "studio.module.artifactReplay.coreObject",
    action: "studio.module.artifactReplay.action"
  },
  "/studio/quality": {
    title: "studio.module.qualityGate.title",
    description: "studio.module.qualityGate.description",
    coreObject: "studio.module.qualityGate.coreObject",
    action: "studio.module.qualityGate.action"
  },
  "/studio/review": {
    title: "studio.module.humanReview.title",
    description: "studio.module.humanReview.description",
    coreObject: "studio.module.humanReview.coreObject",
    action: "studio.module.humanReview.action"
  },
  "/studio/sources": {
    title: "studio.module.sources.title",
    description: "studio.module.sources.description",
    coreObject: "studio.module.sources.coreObject",
    action: "studio.module.sources.action"
  }
}
