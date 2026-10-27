import { AlertCircle, Database, Loader2 } from "lucide-react"
import { ErrorState } from "@/components/common/error-state"
import { Badge } from "@/components/ui/badge"
import { ProjectEmptyText } from "@/features/projects/components/project-page-layout"
import { dataStateBadgeVariant } from "@/features/projects/components/project-tone"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { ProjectsApiMeta } from "@/types/projects"

// Projects state surfaces only use shared theme tokens (border, card, muted, warning);
// light/dark ownership stays in the design system.

export function ProjectLoadingState({ title = "Loading Projects" }: { title?: string }) {
  const { t } = useI18n()
  return (
    <div className="grid min-h-64 place-items-center px-4 py-12 text-center" role="status" aria-busy="true">
      <div>
        <Loader2 className="mx-auto size-5 animate-spin text-muted-foreground" strokeWidth={1.8} aria-hidden="true" />
        <p className="mt-3 text-body2 font-medium text-foreground">{title}</p>
        <p className="mt-1 text-body2 text-muted-foreground">{t("projects.state.loadingDescription")}</p>
      </div>
    </div>
  )
}

export function ProjectErrorState({ message, onRetry }: { message?: string; onRetry?: () => void }) {
  const { t } = useI18n()
  return (
    <div className="px-4 py-5 md:px-6">
      <ErrorState title={t("projects.state.errorTitle")} message={message ?? t("projects.state.errorMessage")} onRetry={onRetry} />
    </div>
  )
}

export function ProjectEmptyState({ title = "No Real Project Radar Data" }: { title?: string }) {
  const { t } = useI18n()
  return <ProjectEmptyText title={title === "No Real Project Radar Data" ? t("projects.state.emptyTitle") : title} description={t("projects.state.emptyDescription")} />
}

export function ProjectDegradedNotice({ meta }: { meta: ProjectsApiMeta }) {
  const { t, dataState } = useI18n()
  if (meta.data_state === "ready" && meta.notices.length === 0) return null

  const latestNotice = meta.notices[meta.notices.length - 1]
  const text = meta.data_state === "empty" ? t("projects.state.emptyDescription") : latestNotice ?? t("projects.state.realOnly")

  return (
    <div className="flex gap-3 rounded-xl border border-warning/30 bg-warning/10 px-4 py-3 text-foreground" role="status">
      <AlertCircle className="mt-0.5 size-4 shrink-0 text-warning-text" strokeWidth={1.8} aria-hidden="true" />
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-body2 font-semibold">{t("projects.state.dataState")}</p>
          <Badge variant={dataStateBadgeVariant(meta.data_state)}>{dataState(meta.data_state)}</Badge>
          <Badge variant="muted">{meta.source}</Badge>
        </div>
        <p className="mt-1 break-words text-body2 text-muted-foreground">{text}</p>
      </div>
    </div>
  )
}

export function ProjectSourceLine({ meta }: { meta: ProjectsApiMeta }) {
  const { t } = useI18n()
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-label text-muted-foreground">
      <Database className="size-3.5 shrink-0" strokeWidth={1.8} aria-hidden="true" />
      <span className="break-all">{t("projects.state.source", { source: meta.source })}</span>
      {meta.source_run_id ? <span className="break-all">{t("projects.state.run", { run: meta.source_run_id })}</span> : null}
      {meta.generated_at ? <span>{t("projects.state.generated", { time: meta.generated_at })}</span> : null}
    </div>
  )
}
