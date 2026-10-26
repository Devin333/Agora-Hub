import { BarChart3, FileText, Lightbulb, ListChecks } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatDataState } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioBoardOutputViewModel } from "@/types/board"

const qualityVariant = {
  ready: "success",
  partial: "warning",
  fallback: "muted"
} as const

export function BoardOutputPanel({ output }: { output: StudioBoardOutputViewModel }) {
  const { locale, t } = useI18n()
  return (
    <section className="space-y-4">
      <div className="grid gap-4 md:grid-cols-4">
        <MetricTile label={t("studio.boards.cards")} value={output.stats.cardCount} />
        <MetricTile label={t("studio.boards.insights")} value={output.stats.insightCount} />
        <MetricTile label={t("studio.boards.relations")} value={output.stats.relationCount} />
        <MetricTile label={t("studio.boards.quality")} value={output.quality.score ?? formatDataState(locale, output.quality.status)} />
      </div>

      <Card>
        <CardHeader className="flex-row items-center justify-between gap-4">
          <div>
            <CardTitle>{t("studio.boards.qualitySummary")}</CardTitle>
            <p className="mt-1 text-sm text-muted-foreground">{output.quality.label}</p>
          </div>
          <Badge variant={qualityVariant[output.quality.status]}>{formatDataState(locale, output.quality.status)}</Badge>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-2">
            {output.quality.checks.map((check) => (
              <Badge key={check} variant="muted">
                {check}
              </Badge>
            ))}
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(0,0.9fr)]">
        <Card>
          <CardHeader className="flex-row items-center gap-3">
            <ListChecks className="size-5 text-link" />
            <CardTitle>{t("studio.boards.cards")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {output.cards.length ? (
              output.cards.map((card) => (
                <article key={card.id} className="rounded-md border border-border p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h3 className="text-sm font-semibold text-foreground">{card.title}</h3>
                      {card.subtitle ? <p className="mt-1 text-xs text-muted-foreground">{card.subtitle}</p> : null}
                    </div>
                    {card.score !== undefined ? <Badge variant="accent">{t("studio.boards.score", { score: card.score })}</Badge> : null}
                  </div>
                  <p className="mt-3 text-sm leading-6 text-muted-foreground">{card.summary}</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {card.badges.slice(0, 5).map((badge) => (
                      <Badge key={`${card.id}-${badge.label}-${badge.value ?? ""}`} variant="muted">
                        {badge.label}
                        {badge.value ? ` ${badge.value}` : ""}
                      </Badge>
                    ))}
                  </div>
                  {card.rankingReason ? (
                    <p className="mt-3 text-xs leading-5 text-muted-foreground">{card.rankingReason}</p>
                  ) : null}
                </article>
              ))
            ) : (
              <p className="text-sm text-muted-foreground">{t("studio.boards.noCards")}</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex-row items-center gap-3">
            <Lightbulb className="size-5 text-link" />
            <CardTitle>{t("studio.boards.insights")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {output.insights.length ? (
              output.insights.map((insight) => (
                <article key={insight.id} className="rounded-md border border-border p-4">
                  <h3 className="text-sm font-semibold text-foreground">{insight.title}</h3>
                  <p className="mt-2 text-sm leading-6 text-muted-foreground">{insight.summary}</p>
                  {insight.insightType ? (
                    <Badge className="mt-3" variant="muted">
                      {insight.insightType}
                    </Badge>
                  ) : null}
                </article>
              ))
            ) : (
              <p className="text-sm text-muted-foreground">{t("studio.boards.noInsights")}</p>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader className="flex-row items-center gap-3">
            <FileText className="size-5 text-link" />
            <CardTitle>{t("studio.boards.detailPages")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {output.detailPages.map((page) => (
              <div key={page.id} className="rounded-md border border-border p-3">
                <p className="text-sm font-medium text-foreground">{page.title}</p>
                <p className="mt-1 text-sm text-muted-foreground">{page.summary}</p>
                <p className="mt-2 text-xs text-muted-foreground">{t("studio.boards.sectionCount", { count: page.sectionCount })}</p>
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex-row items-center gap-3">
            <BarChart3 className="size-5 text-link" />
            <CardTitle>{t("studio.boards.sections")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {output.sections.map((section) => (
              <div key={section.title} className="rounded-md border border-border p-3">
                <p className="text-sm font-medium text-foreground">{section.title}</p>
                {section.content ? <p className="mt-1 text-sm text-muted-foreground">{section.content}</p> : null}
                <p className="mt-2 text-xs text-muted-foreground">
                  {t("studio.boards.sectionStats", { cards: section.cardCount, insights: section.insightCount, metrics: section.metricCount })}
                </p>
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </section>
  )
}

function MetricTile({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-md border border-border bg-card p-4">
      <p className="text-xs font-semibold uppercase tracking-normal text-muted-foreground">{label}</p>
      <p className="mt-2 text-2xl font-semibold text-foreground">{value}</p>
    </div>
  )
}
