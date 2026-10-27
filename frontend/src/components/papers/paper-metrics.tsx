import type { ReactNode } from "react"
import { Github, Quote, Star, TrendingUp } from "lucide-react"
import { papersCopy, t } from "@/lib/papers/copy"
import { formatCompactNumber } from "@/lib/papers/format"
import type { Locale, Paper } from "@/lib/papers/types"

export function PaperMetrics({ paper, locale }: { paper: Paper; locale: Locale }) {
  const hasGithubStars = typeof paper.githubStars === "number"
  const hasCitations = typeof paper.citationCount === "number"
  const hasMomentum = typeof paper.githubMomentum === "number"

  if (!hasGithubStars && !hasCitations && !hasMomentum) {
    return null
  }

  return (
    <div className="grid gap-3">
      {hasGithubStars ? (
        <Metric icon={<Github className="size-4" strokeWidth={1.8} aria-hidden="true" />} value={formatCompactNumber(paper.githubStars)} label={t(papersCopy.githubStars, locale)} />
      ) : null}
      {hasCitations ? (
        <Metric icon={<Quote className="size-4" strokeWidth={1.8} aria-hidden="true" />} value={formatCompactNumber(paper.citationCount)} label={t(papersCopy.citations, locale)} />
      ) : null}
      {hasMomentum ? (
        <Metric icon={<TrendingUp className="size-4" strokeWidth={1.8} aria-hidden="true" />} value={formatCompactNumber(paper.githubMomentum)} label={t(papersCopy.githubMomentum, locale)} tone="success" />
      ) : null}
    </div>
  )
}

function Metric({
  icon,
  value,
  label,
  tone = "neutral"
}: {
  icon: ReactNode
  value: string
  label: string
  tone?: "neutral" | "success"
}) {
  return (
    <div className="flex items-center gap-2.5 text-left">
      <span className={tone === "success" ? "text-success" : "text-muted-foreground"}>{icon}</span>
      <span className="min-w-0">
        <span className="flex items-center gap-1.5 text-body1 font-semibold tabular-nums text-foreground">
          {value}
          {label.includes("GitHub") ? <Star className="size-3 text-warning" strokeWidth={1.8} aria-hidden="true" /> : null}
        </span>
        <span className="block truncate text-caption text-muted-foreground">
          {label}
        </span>
      </span>
    </div>
  )
}
