import Link from "next/link"
import type { ReactNode } from "react"
import { ArrowLeft, ExternalLink, GitFork, Layers3, MessageSquare, Newspaper, Star, TrendingUp } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { formatCompactNumber, formatSignedNumber, labelize } from "@/features/projects/components/project-format"
import {
  ProjectList,
  ProjectMetric,
  ProjectPage,
  ProjectPageBody,
  ProjectPageHeader,
  ProjectPanel,
  projectListRowClassName,
} from "@/features/projects/components/project-page-layout"
import type { EvidenceRef, ProjectItem, RelatedCommunityRef, RelatedNewsRef, RelatedPaperRef } from "@/types/projects"

const iconProps = { className: "size-4", strokeWidth: 1.8, "aria-hidden": true } as const

export function ProjectDetailPanel({ project }: { project: ProjectItem }) {
  return (
    <ProjectPage>
      <ProjectDetailHeader project={project} backHref="/tech/repos" />
      <ProjectPageBody>
        <ProjectDetailContent project={project} />
      </ProjectPageBody>
    </ProjectPage>
  )
}

export function ProjectDetailHeader({ project, backHref }: { project: ProjectItem; backHref?: string }) {
  return (
    <ProjectPageHeader
      eyebrow={`Project Radar / ${project.fullName}`}
      title={project.name}
      subtitle={project.description}
      actions={
        <>
          {backHref ? (
            <Button asChild variant="outline" size="sm">
              <Link href={backHref}>
                <ArrowLeft strokeWidth={1.8} />
                Back to radar
              </Link>
            </Button>
          ) : null}
          <Button asChild size="sm">
            <a href={project.repoUrl} target="_blank" rel="noreferrer">
              <ExternalLink strokeWidth={1.8} />
              Open repo
            </a>
          </Button>
        </>
      }
    >
      <ProjectBadges project={project} />
    </ProjectPageHeader>
  )
}

export function ProjectBadges({ project, showLicense = true }: { project: ProjectItem; showLicense?: boolean }) {
  const updatedAt = project.pushedAt ?? project.updatedAt
  return (
    <div className="flex flex-wrap gap-1.5">
      <Badge variant={project.maturity ? "info" : "muted"}>{project.maturity ? labelize(project.maturity) : "Maturity unavailable"}</Badge>
      {project.language ? <Badge variant="muted">{project.language}</Badge> : null}
      {showLicense && project.license ? <Badge variant="muted">{project.license}</Badge> : null}
      {showLicense && updatedAt ? <Badge variant="muted">Updated {formatDate(updatedAt)}</Badge> : null}
    </div>
  )
}

export function ProjectDetailContent({ project }: { project: ProjectItem }) {
  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_20rem]">
      <section className="min-w-0 space-y-5">
        <section className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          <ProjectMetric size="lg" label="Stars" value={formatCompactNumber(project.stars)} icon={<Star {...iconProps} />} />
          <ProjectMetric size="lg" label="Forks" value={formatCompactNumber(project.forks)} icon={<GitFork {...iconProps} />} />
          <ProjectMetric size="lg" label="Star delta" value={formatSignedNumber(project.starGrowth7d ?? project.starGrowth24h)} icon={<TrendingUp {...iconProps} />} />
          <ProjectMetric
            size="lg"
            label="Evidence"
            value={String(project.relationCounts.papers + project.relationCounts.news + project.relationCounts.community)}
            icon={<Layers3 {...iconProps} />}
          />
        </section>

        <ProjectPanel title="Engineering read">
          <div className="space-y-4 text-body2 text-muted-foreground">
            <TextBlock title="Problem solved" value={project.problemSolved ?? project.description} />
            <TextBlock title="Why it matters" value={project.whyItMatters ?? "No extra rationale is present in the current Project Radar artifact."} />
          </div>
        </ProjectPanel>

        <ProjectPanel title="Trend and adoption signals">
          <div className="grid gap-x-6 md:grid-cols-2">
            <Fact label="Trend score" value={formatScore(project.scores.trendScore)} />
            <Fact label="Star velocity" value={formatScore(project.scores.starVelocityScore)} />
            <Fact label="Activity score" value={formatScore(project.scores.activityScore)} />
            <Fact label="Freshness score" value={formatScore(project.scores.freshnessScore)} />
            <Fact label="Adoption score" value={formatScore(project.scores.adoptionScore)} />
            <Fact label="Evidence score" value={formatScore(project.scores.evidenceScore)} />
          </div>
        </ProjectPanel>

        <ReferenceList title="Related papers" emptyDescription="No related papers are present in the current artifact." items={project.relatedPapers ?? []} />
        <ReferenceList title="Related news" emptyDescription="No related news references are present in the current artifact." items={project.relatedNews ?? []} />
        <ReferenceList
          title="Community discussion"
          emptyDescription="No related community discussion is present in the current artifact."
          items={project.relatedCommunityTopics ?? []}
        />
        <EvidenceList refs={project.sourceRefs ?? []} />
      </section>

      <aside className="min-w-0 space-y-4">
        <ProjectPanel title="Repository profile">
          <div>
            <Fact label="Repository" value={project.fullName} href={project.repoUrl} />
            <Fact label="Homepage" value={project.homepageUrl} href={project.homepageUrl} />
            <Fact label="Owner" value={project.owner} />
            <Fact label="Language" value={project.language} />
            <Fact label="License" value={project.license} />
            <Fact label="Open issues" value={formatOptionalNumber(project.openIssues)} />
            <Fact label="Created" value={formatDate(project.createdAt)} />
            <Fact label="Updated" value={formatDate(project.updatedAt ?? project.pushedAt ?? project.lastPushedAt)} />
            <Fact label="First seen" value={formatDate(project.firstSeenAt)} />
          </div>
        </ProjectPanel>
        <ProjectPanel title="Topics">
          <div className="flex flex-wrap gap-1.5">
            {project.categoryRefs.map((category) => (
              <Badge key={category.category}>{category.label}</Badge>
            ))}
            {project.topics.map((topic) => (
              <Badge key={topic} variant="muted">
                {topic}
              </Badge>
            ))}
            {!project.categoryRefs.length && !project.topics.length ? <p className="text-body2 text-muted-foreground">No topic labels are available.</p> : null}
          </div>
        </ProjectPanel>

        <ProjectPanel title="Relation counts">
          <div>
            <RelationLine icon={<Layers3 {...iconProps} />} label="Papers" value={project.relationCounts.papers} />
            <RelationLine icon={<Newspaper {...iconProps} />} label="News" value={project.relationCounts.news} />
            <RelationLine icon={<MessageSquare {...iconProps} />} label="Community" value={project.relationCounts.community} />
          </div>
        </ProjectPanel>
      </aside>
    </div>
  )
}

function TextBlock({ title, value }: { title: string; value: string }) {
  return (
    <div>
      <h3 className="text-body2 font-semibold text-foreground">{title}</h3>
      <p className="mt-1 break-words">{value}</p>
    </div>
  )
}

function ReferenceList({
  title,
  emptyDescription,
  items,
}: {
  title: string
  emptyDescription: string
  items: Array<RelatedPaperRef | RelatedNewsRef | RelatedCommunityRef>
}) {
  return (
    <section className="space-y-2">
      <h2 className="text-body1 font-semibold text-foreground">{title}</h2>
      {items.length ? (
        <ProjectList>
          {items.map((item) => (
            <a
              key={`${item.title}-${item.url ?? item.id ?? ""}`}
              href={item.url ?? "#"}
              target={item.url ? "_blank" : undefined}
              rel={item.url ? "noreferrer" : undefined}
              className={projectListRowClassName}
            >
              <span className="break-words font-medium text-foreground">{item.title}</span>
              {"sourceName" in item && item.sourceName ? <span className="ml-2 text-muted-foreground">{item.sourceName}</span> : null}
            </a>
          ))}
        </ProjectList>
      ) : (
        <p className="text-body2 text-muted-foreground">{emptyDescription}</p>
      )}
    </section>
  )
}

function EvidenceList({ refs }: { refs: EvidenceRef[] }) {
  return (
    <section className="space-y-2">
      <h2 className="text-body1 font-semibold text-foreground">Evidence refs</h2>
      {refs.length ? (
        <ProjectList>
          {refs.map((ref) => (
            <a
              key={`${ref.id ?? ""}-${ref.url ?? ref.sourceUrl ?? ""}`}
              href={ref.url ?? ref.sourceUrl ?? "#"}
              target={ref.url || ref.sourceUrl ? "_blank" : undefined}
              rel={ref.url || ref.sourceUrl ? "noreferrer" : undefined}
              className={projectListRowClassName}
            >
              <span className="break-words font-medium text-foreground">{ref.title ?? ref.sourceName ?? ref.url ?? ref.sourceUrl ?? "Evidence"}</span>
              <span className="mt-0.5 block text-label text-muted-foreground">{ref.sourceType ?? ref.reliability ?? "source"}</span>
            </a>
          ))}
        </ProjectList>
      ) : (
        <p className="text-body2 text-muted-foreground">This project has no displayable evidence refs in the current artifact.</p>
      )}
    </section>
  )
}

function Fact({ label, value, href }: { label: string; value?: string; href?: string }) {
  if (!value) return null
  return (
    <div className="flex items-start justify-between gap-4 border-b border-border py-2 text-body2 last:border-0">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      {href ? (
        <a href={href} target="_blank" rel="noreferrer" className="min-w-0 truncate text-right text-link hover:underline">
          {value}
        </a>
      ) : (
        <span className="min-w-0 truncate text-right text-foreground">{value}</span>
      )}
    </div>
  )
}

function RelationLine({ icon, label, value }: { icon: ReactNode; label: string; value: number }) {
  return (
    <div className="flex items-center justify-between border-b border-border py-2 text-body2 last:border-0">
      <span className="inline-flex items-center gap-2 text-muted-foreground">
        {icon}
        {label}
      </span>
      <span className="font-semibold text-foreground">{value}</span>
    </div>
  )
}

function formatOptionalNumber(value: number | undefined): string | undefined {
  return value === undefined ? undefined : formatCompactNumber(value)
}

function formatScore(value: number | undefined): string {
  if (value === undefined) return "-"
  return value <= 1 ? `${Math.round(value * 100)}%` : value.toFixed(1)
}

function formatDate(value: string | undefined): string | undefined {
  if (!value) return undefined
  const time = Date.parse(value)
  if (!Number.isFinite(time)) return value
  return new Intl.DateTimeFormat("en", { month: "short", day: "numeric", year: "numeric" }).format(time)
}
