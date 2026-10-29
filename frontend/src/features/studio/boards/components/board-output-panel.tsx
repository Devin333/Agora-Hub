import type { ReactNode } from "react"
import { BarChart3, FileText, Lightbulb, ListChecks } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { StudioMetricCard, StudioMetricGrid, StudioPanel, type StudioIcon } from "@/features/studio/shared/components/studio-dashboard"
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
    <section className="space-y-5">
      <StudioMetricGrid className="xl:grid-cols-4 2xl:grid-cols-4">
        <StudioMetricCard label={t("studio.boards.cards")} value={output.stats.cardCount} />
        <StudioMetricCard label={t("studio.boards.insights")} value={output.stats.insightCount} />
        <StudioMetricCard label={t("studio.boards.relations")} value={output.stats.relationCount} />
        <StudioMetricCard label={t("studio.boards.quality")} value={output.quality.score ?? formatDataState(locale, output.quality.status)} />
      </StudioMetricGrid>

      <StudioPanel
        title={t("studio.boards.qualitySummary")}
        description={output.quality.label}
        actions={<Badge variant={qualityVariant[output.quality.status]}>{formatDataState(locale, output.quality.status)}</Badge>}
      >
        <div className="flex flex-wrap gap-1.5">
          {output.quality.checks.map((check) => (
            <Badge key={check} variant="muted">
              {check}
            </Badge>
          ))}
        </div>
      </StudioPanel>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1.35fr)_minmax(0,0.9fr)]">
        <OutputSection icon={ListChecks} title={t("studio.boards.cards")} empty={t("studio.boards.noCards")} isEmpty={!output.cards.length}>
          {output.cards.map((card) => (
            <article key={card.id} className="px-4 py-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="text-[13.5px] font-medium text-foreground">{card.title}</h3>
                  {card.subtitle ? <p className="mt-0.5 text-label text-muted-foreground">{card.subtitle}</p> : null}
                </div>
                {card.score !== undefined ? <Badge variant="accent">{t("studio.boards.score", { score: card.score })}</Badge> : null}
              </div>
              <p className="mt-2 text-body2 text-muted-foreground">{card.summary}</p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {card.badges.slice(0, 5).map((badge) => (
                  <Badge key={`${card.id}-${badge.label}-${badge.value ?? ""}`} variant="muted">
                    {badge.label}
                    {badge.value ? ` ${badge.value}` : ""}
                  </Badge>
                ))}
              </div>
              {card.rankingReason ? <p className="mt-2 text-label text-faint-foreground">{card.rankingReason}</p> : null}
            </article>
          ))}
        </OutputSection>

        <OutputSection icon={Lightbulb} title={t("studio.boards.insights")} empty={t("studio.boards.noInsights")} isEmpty={!output.insights.length}>
          {output.insights.map((insight) => (
            <article key={insight.id} className="px-4 py-3">
              <h3 className="text-[13.5px] font-medium text-foreground">{insight.title}</h3>
              <p className="mt-1 text-body2 text-muted-foreground">{insight.summary}</p>
              {insight.insightType ? (
                <Badge className="mt-2" variant="muted">
                  {insight.insightType}
                </Badge>
              ) : null}
            </article>
          ))}
        </OutputSection>
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <OutputSection icon={FileText} title={t("studio.boards.detailPages")} isEmpty={!output.detailPages.length}>
          {output.detailPages.map((page) => (
            <div key={page.id} className="px-4 py-3">
              <p className="text-[13.5px] font-medium text-foreground">{page.title}</p>
              <p className="mt-0.5 text-body2 text-muted-foreground">{page.summary}</p>
              <p className="mt-1 text-label text-faint-foreground">{t("studio.boards.sectionCount", { count: page.sectionCount })}</p>
            </div>
          ))}
        </OutputSection>

        <OutputSection icon={BarChart3} title={t("studio.boards.sections")} isEmpty={!output.sections.length}>
          {output.sections.map((section) => (
            <div key={section.title} className="px-4 py-3">
              <p className="text-[13.5px] font-medium text-foreground">{section.title}</p>
              {section.content ? <p className="mt-0.5 text-body2 text-muted-foreground">{section.content}</p> : null}
              <p className="mt-1 text-label text-faint-foreground">
                {t("studio.boards.sectionStats", { cards: section.cardCount, insights: section.insightCount, metrics: section.metricCount })}
              </p>
            </div>
          ))}
        </OutputSection>
      </div>
    </section>
  )
}

/** Titled section whose rows render as a rakazo divided list. */
function OutputSection({
  icon: Icon,
  title,
  empty,
  isEmpty,
  children
}: {
  icon: StudioIcon
  title: string
  empty?: string
  isEmpty: boolean
  children: ReactNode
}) {
  return (
    <section className="min-w-0 space-y-3">
      <div className="flex items-center gap-2">
        <Icon size={16} strokeWidth={1.8} aria-hidden="true" className="text-muted-foreground" />
        <h2 className="text-[15px] font-semibold text-foreground">{title}</h2>
      </div>
      {isEmpty ? (
        empty ? <p className="text-body2 text-muted-foreground">{empty}</p> : null
      ) : (
        <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">{children}</div>
      )}
    </section>
  )
}
