import Link from "next/link"
import type { ReactNode } from "react"
import { Clock3, ExternalLink, GitFork, Layers3, MessageSquare, Newspaper, Star, TrendingUp } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { formatCompactNumber, formatSignedNumber, labelize } from "@/features/projects/components/project-format"
import { ProjectMetric, projectCardClassName } from "@/features/projects/components/project-page-layout"
import type { ProjectItem } from "@/types/projects"

const iconProps = { className: "size-3.5", strokeWidth: 1.8, "aria-hidden": true } as const

export function ProjectCard({ project, detailHref }: { project: ProjectItem; detailHref?: string }) {
  const href = detailHref ?? `/tech/repos?project=${encodeURIComponent(project.slug)}`
  const updatedAt = project.pushedAt ?? project.updatedAt ?? project.lastPushedAt

  return (
    <article className={projectCardClassName}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate font-mono text-caption text-faint-foreground">{project.fullName}</p>
          <h2 className="mt-1 truncate text-[15px] font-semibold text-foreground">
            <Link href={href} className="rounded-sm outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50">
              {project.name}
            </Link>
          </h2>
        </div>
        <Badge variant={project.maturity ? "info" : "muted"}>{project.maturity ? labelize(project.maturity) : "Maturity unavailable"}</Badge>
      </div>

      <p className="mt-2 line-clamp-3 min-h-[3.375rem] text-body2 text-muted-foreground">{project.description}</p>

      <div className="mt-3 grid grid-cols-3 gap-2">
        <ProjectMetric icon={<Star {...iconProps} />} label="Stars" value={formatCompactNumber(project.stars)} />
        <ProjectMetric icon={<GitFork {...iconProps} />} label="Forks" value={formatCompactNumber(project.forks)} />
        <ProjectMetric icon={<TrendingUp {...iconProps} />} label="Delta" value={formatSignedNumber(project.starGrowth7d ?? project.starGrowth24h)} />
      </div>

      <div className="mt-3 flex flex-wrap gap-1.5">
        {project.language ? <Badge variant="muted">{project.language}</Badge> : null}
        {updatedAt ? (
          <Badge variant="muted">
            <Clock3 strokeWidth={1.8} aria-hidden="true" />
            {formatShortDate(updatedAt)}
          </Badge>
        ) : null}
        {project.categoryRefs.slice(0, 2).map((category) => (
          <Badge key={category.category}>{category.label}</Badge>
        ))}
        {project.topics.slice(0, 3).map((topic) => (
          <Badge key={topic} variant="muted">
            {topic}
          </Badge>
        ))}
      </div>

      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-label text-muted-foreground">
        <RelationMetric icon={<Layers3 {...iconProps} />} label="Papers" value={project.relationCounts.papers} />
        <RelationMetric icon={<Newspaper {...iconProps} />} label="News" value={project.relationCounts.news} />
        <RelationMetric icon={<MessageSquare {...iconProps} />} label="Community" value={project.relationCounts.community} />
      </div>

      <div className="mt-auto pt-4">
        <div className="flex items-center justify-between gap-2 border-t border-border pt-3">
          <Button asChild variant="outline" size="sm">
            <a href={project.repoUrl} target="_blank" rel="noreferrer">
              <ExternalLink strokeWidth={1.8} />
              Open repo
            </a>
          </Button>
          <Button asChild size="sm">
            <Link href={href}>View detail</Link>
          </Button>
        </div>
      </div>
    </article>
  )
}

function RelationMetric({ icon, label, value }: { icon: ReactNode; label: string; value: number }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      {icon}
      <span className="font-semibold text-foreground">{value}</span>
      <span>{label}</span>
    </span>
  )
}

function formatShortDate(value: string): string {
  const time = Date.parse(value)
  if (!Number.isFinite(time)) return value
  return new Intl.DateTimeFormat("en", { month: "short", day: "numeric", year: "numeric" }).format(time)
}
