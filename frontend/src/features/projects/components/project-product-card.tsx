import Link from "next/link"
import type { ReactNode } from "react"
import { ArrowRight, ExternalLink, GitFork, Layers3, Star, TrendingUp } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { formatCompactNumber, formatDate, formatScore, labelize } from "@/features/projects/components/project-format"
import { ProjectMetric, projectCardClassName } from "@/features/projects/components/project-page-layout"
import { cn } from "@/lib/utils"
import type { ProjectsApiProject } from "@/types/projects"

const iconProps = { className: "size-3.5", strokeWidth: 1.8, "aria-hidden": true } as const
const missingDescription = "No description was available in the real Project Radar artifact."

export function ProjectProductCard({
  project,
  compact = false,
}: {
  project: ProjectsApiProject
  compact?: boolean
}) {
  const href = projectHref(project)

  return (
    <article className={projectCardClassName}>
      <CardHeading project={project} href={href} />
      <p className={cn("mt-2 text-body2 text-muted-foreground", compact ? "line-clamp-2" : "line-clamp-3 min-h-[3.375rem]")}>
        {project.description ?? project.tagline ?? missingDescription}
      </p>
      <CardSignals project={project} />
      <CardTags project={project} limit={compact ? 2 : 4} />
      <CardFooter project={project} href={href} />
    </article>
  )
}

export function RankedProjectCard({
  project,
  rank,
  rankReason,
}: {
  project: ProjectsApiProject
  rank: number
  rankReason?: string
}) {
  const href = projectHref(project)
  const score = projectScore(project)

  return (
    <article className={projectCardClassName}>
      <CardHeading
        project={project}
        href={href}
        leading={
          <span className="grid size-7 shrink-0 place-items-center rounded-md bg-accent text-label font-semibold text-accent-foreground">#{rank}</span>
        }
      />
      {rankReason ? <p className="mt-2 line-clamp-2 text-label text-muted-foreground">{rankReason}</p> : null}
      <p className="mt-2 line-clamp-2 text-body2 text-muted-foreground">{project.description ?? project.tagline ?? missingDescription}</p>

      <div className="mt-3">
        <div className="mb-1 flex items-center justify-between text-label">
          <span className="text-muted-foreground">Heat Score</span>
          <span className="font-semibold text-foreground">{formatScore(score ?? undefined)}</span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-muted" role="presentation">
          <div className="h-full rounded-full bg-warning" style={{ width: `${Math.min(score ?? 0, 1) * 100}%` }} />
        </div>
      </div>

      <CardSignals project={project} />
      <CardTags project={project} limit={2} />
      <CardFooter project={project} href={href} />
    </article>
  )
}

function CardHeading({ project, href, leading }: { project: ProjectsApiProject; href: string; leading?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2">
          {leading}
          <p className="truncate font-mono text-caption text-faint-foreground">{project.github_url ?? project.canonical_url ?? project.id}</p>
        </div>
        <h2 className="mt-1 truncate text-[15px] font-semibold text-foreground">
          <Link href={href} className="rounded-sm outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50">
            {project.name}
          </Link>
        </h2>
      </div>
      <Badge variant={project.project_type ? "info" : "muted"}>{labelize(project.project_type)}</Badge>
    </div>
  )
}

function CardSignals({ project }: { project: ProjectsApiProject }) {
  return (
    <div className="mt-3 grid grid-cols-3 gap-2">
      <ProjectMetric label="Stars" value={formatCompactNumber(numberMetric(project.metric_summary.github_stars))} icon={<Star {...iconProps} />} />
      <ProjectMetric label="Forks" value={formatCompactNumber(numberMetric(project.metric_summary.github_forks))} icon={<GitFork {...iconProps} />} />
      <ProjectMetric label="Delta" value={formatCompactNumber(numberMetric(project.metric_summary.stars_delta_7d))} icon={<TrendingUp {...iconProps} />} />
    </div>
  )
}

function CardTags({ project, limit }: { project: ProjectsApiProject; limit: number }) {
  return (
    <div className="mt-3 flex flex-wrap gap-1.5">
      {project.category ? <Badge>{labelize(project.category)}</Badge> : null}
      {project.updated_at ? <Badge variant="muted">Updated {formatDate(project.updated_at)}</Badge> : null}
      {project.tags.slice(0, limit).map((tag) => (
        <Badge key={tag} variant="muted">
          {labelize(tag)}
        </Badge>
      ))}
    </div>
  )
}

function CardFooter({ project, href }: { project: ProjectsApiProject; href: string }) {
  const score = projectScore(project)
  return (
    <div className="mt-auto pt-4">
      <div className="flex items-center justify-between gap-3 border-t border-border pt-3 text-label text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <Layers3 {...iconProps} />
          {project.source_count} sources
        </span>
        <span>Score {formatScore(score ?? undefined)}</span>
      </div>
      <div className="mt-3 flex items-center justify-between gap-2">
        <Button asChild variant="outline" size="sm" disabled={!project.github_url}>
          <a href={project.github_url ?? project.canonical_url ?? href} target="_blank" rel="noreferrer">
            <ExternalLink strokeWidth={1.8} />
            Open source
          </a>
        </Button>
        <Button asChild size="sm">
          <Link href={href}>
            View detail
            <ArrowRight strokeWidth={1.8} />
          </Link>
        </Button>
      </div>
    </div>
  )
}

function projectHref(project: ProjectsApiProject): string {
  return `/projects/${encodeURIComponent(project.slug)}`
}

function projectScore(project: ProjectsApiProject): number | null | undefined {
  return project.hot_score ?? project.rising_score
}

function numberMetric(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined
}
