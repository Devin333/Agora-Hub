import Link from "next/link"
import { ArrowRight, Boxes, Clock, FileInput, FileOutput } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { formatDataState } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import type {
  StudioBoardDefinition,
  StudioBoardSummary,
  StudioBoardType
} from "@/types/board"

const statusVariant = {
  ready: "success",
  partial: "warning",
  fallback: "muted"
} as const

type BoardCardGridProps = {
  summaries: StudioBoardSummary[]
  definitions: Record<StudioBoardType, StudioBoardDefinition>
}

/** Rakazo card grid of business boards; each card links to its board detail page. */
export function BoardCardGrid({ summaries, definitions }: BoardCardGridProps) {
  const { locale, t } = useI18n()
  return (
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {summaries.map((summary) => {
        const definition = definitions[summary.boardType]
        return (
          <Link
            key={summary.boardType}
            href={`/studio/boards/${summary.boardType}`}
            className="group flex min-h-[280px] flex-col gap-4 rounded-2xl border border-border bg-card p-4 text-foreground outline-none transition-colors hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 items-center gap-3">
                <span className="grid size-9 shrink-0 place-items-center rounded-[10px] bg-accent text-accent-foreground">
                  <Boxes size={16} strokeWidth={1.8} aria-hidden="true" />
                </span>
                <div className="min-w-0">
                  <h2 className="truncate text-[15px] font-semibold">{summary.title}</h2>
                  <p className="mt-0.5 truncate font-mono text-label text-muted-foreground">{summary.boardType}</p>
                </div>
              </div>
              <Badge variant={statusVariant[summary.status]}>{formatDataState(locale, summary.status)}</Badge>
            </div>
            {summary.description ? <p className="line-clamp-3 text-[13px] leading-relaxed text-muted-foreground">{summary.description}</p> : null}

            <div className="space-y-3 text-body2">
              <InfoLine icon={FileInput} label={t("studio.boards.inputObject")} value={definition.inputObject} />
              <InfoLine icon={FileOutput} label={t("studio.boards.outputObject")} value={definition.outputObject} />
              <InfoLine icon={Clock} label={t("studio.boards.lastRun")} value={summary.lastRunId ?? t("studio.boards.waitingRunIndex")} />
              <div>
                <p className="text-label text-muted-foreground">{t("studio.boards.qualityStatus")}</p>
                <p className="mt-0.5 tabular-nums text-foreground">
                  {summary.qualityScore !== undefined ? `${summary.qualityScore}/100` : formatDataState(locale, summary.status)}
                </p>
              </div>
            </div>

            <div className="mt-auto flex items-center justify-between gap-3 text-body2 font-medium text-link">
              <span>{t("studio.boards.openBoard")}</span>
              <ArrowRight size={15} strokeWidth={1.8} aria-hidden="true" className="transition-transform group-hover:translate-x-0.5" />
            </div>
          </Link>
        )
      })}
    </section>
  )
}

function InfoLine({
  icon: Icon,
  label,
  value
}: {
  icon: typeof FileInput
  label: string
  value: string
}) {
  return (
    <div className="flex items-start gap-2.5">
      <Icon size={15} strokeWidth={1.8} aria-hidden="true" className="mt-0.5 shrink-0 text-muted-foreground" />
      <div className="min-w-0">
        <p className="text-label text-muted-foreground">{label}</p>
        <p className="mt-0.5 break-words text-foreground">{value}</p>
      </div>
    </div>
  )
}
