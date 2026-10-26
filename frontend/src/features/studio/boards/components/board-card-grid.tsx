import Link from "next/link"
import { ArrowRight, Boxes, Clock, FileInput, FileOutput } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
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

export function BoardCardGrid({ summaries, definitions }: BoardCardGridProps) {
  const { locale, t } = useI18n()
  return (
    <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
      {summaries.map((summary) => {
        const definition = definitions[summary.boardType]
        return (
          <Link
            key={summary.boardType}
            href={`/studio/boards/${summary.boardType}`}
            className="group focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Card className="grid h-full min-h-[300px] grid-rows-[auto_1fr_auto] transition-colors group-hover:border-link/60 group-hover:bg-secondary/40">
              <CardHeader className="gap-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex min-w-0 items-center gap-3">
                    <span className="flex size-10 shrink-0 items-center justify-center rounded-md border border-border bg-secondary text-link">
                      <Boxes className="size-5" />
                    </span>
                    <div className="min-w-0">
                      <CardTitle className="truncate">{summary.title}</CardTitle>
                      <p className="mt-1 font-mono text-xs text-muted-foreground">{summary.boardType}</p>
                    </div>
                  </div>
                  <Badge variant={statusVariant[summary.status]}>{formatDataState(locale, summary.status)}</Badge>
                </div>
                {summary.description ? (
                  <p className="text-sm leading-6 text-muted-foreground">{summary.description}</p>
                ) : null}
              </CardHeader>

              <CardContent className="space-y-4 text-sm">
                <InfoLine icon={FileInput} label={t("studio.boards.inputObject")} value={definition.inputObject} />
                <InfoLine icon={FileOutput} label={t("studio.boards.outputObject")} value={definition.outputObject} />
                <InfoLine
                  icon={Clock}
                  label={t("studio.boards.lastRun")}
                  value={summary.lastRunId ?? t("studio.boards.waitingRunIndex")}
                />
                <div>
                  <p className="text-xs font-semibold uppercase tracking-normal text-muted-foreground">{t("studio.boards.qualityStatus")}</p>
                  <p className="mt-1 text-foreground">
                    {summary.qualityScore !== undefined ? `${summary.qualityScore}/100` : formatDataState(locale, summary.status)}
                  </p>
                </div>
              </CardContent>

              <div className="flex items-center justify-between gap-3 border-t border-border px-4 py-3 text-sm font-medium text-link">
                <span>{t("studio.boards.openBoard")}</span>
                <ArrowRight className="size-4 transition-transform group-hover:translate-x-0.5" />
              </div>
            </Card>
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
    <div className="flex items-start gap-3">
      <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0">
        <p className="text-xs font-semibold uppercase tracking-normal text-muted-foreground">{label}</p>
        <p className="mt-1 break-words text-foreground">{value}</p>
      </div>
    </div>
  )
}
