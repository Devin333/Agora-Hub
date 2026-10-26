import Link from "next/link"
import type { ReactNode } from "react"
import { ArrowLeft, ExternalLink, GitFork, Layers3, MessageSquare, Newspaper, Star, TrendingUp } from "lucide-react"
import { EmptyState } from "@/components/common/empty-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import type { EvidenceRef, ProjectItem, RelatedCommunityRef, RelatedNewsRef, RelatedPaperRef } from "@/types/projects"

export function ProjectDetailPanel({ project }: { project: ProjectItem }) {
  return (
    <main className="space-y-8">
      <ProjectDetailHeader project={project} backHref="/tech/repos" />
      <ProjectDetailContent project={project} />
    </main>
  )
}

export function ProjectDetailHeader({ project, backHref }: { project: ProjectItem; backHref?: string }) {
  return (
    <header className="grid gap-6 py-8 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end">
      <div className="min-w-0">
        <p className="mb-4 text-xs font-medium uppercase tracking-[0.18em] text-[#334155]/55 dark:text-muted-foreground">
          Project Radar / {project.fullName}
        </p>
        <h1 className="max-w-5xl text-4xl font-black leading-tight tracking-normal text-[#334155] sm:text-5xl dark:text-foreground">
          {project.name}
        </h1>
        <p className="mt-5 max-w-4xl text-base leading-7 text-[#334155]/70 dark:text-muted-foreground">{project.description}</p>
        <div className="mt-5 flex flex-wrap gap-2">
          <Badge variant={project.maturity ? "info" : "muted"}>{project.maturity ? labelize(project.maturity) : "Maturity unavailable"}</Badge>
          {project.language ? <Badge variant="muted">{project.language}</Badge> : null}
          {project.license ? <Badge variant="muted">{project.license}</Badge> : null}
          {project.pushedAt ?? project.updatedAt ? <Badge variant="muted">Updated {formatDate(project.pushedAt ?? project.updatedAt)}</Badge> : null}
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        {backHref ? (
          <Button asChild variant="outline">
            <Link href={backHref}>
              <ArrowLeft className="size-4" />
              Back to radar
            </Link>
          </Button>
        ) : null}
        <Button asChild>
          <a href={project.repoUrl} target="_blank" rel="noreferrer">
            <ExternalLink className="size-4" />
            Open repo
          </a>
        </Button>
      </div>
    </header>
  )
}

export function ProjectDetailContent({ project }: { project: ProjectItem }) {
  return (
    <div className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_22rem]">
      <section className="min-w-0 space-y-6">
        <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <Metric label="Stars" value={formatCompactNumber(project.stars)} icon={<Star className="size-4" />} />
          <Metric label="Forks" value={formatCompactNumber(project.forks)} icon={<GitFork className="size-4" />} />
          <Metric label="Star delta" value={formatSignedNumber(project.starGrowth7d ?? project.starGrowth24h)} icon={<TrendingUp className="size-4" />} />
          <Metric label="Evidence" value={String(project.relationCounts.papers + project.relationCounts.news + project.relationCounts.community)} icon={<Layers3 className="size-4" />} />
        </section>

        <Card className="border-[#dbe3dc] bg-white/85 dark:border-border dark:bg-card">
          <CardHeader>
            <CardTitle>Engineering read</CardTitle>
          </CardHeader>
          <CardContent className="space-y-5 text-sm leading-6 text-[#334155]/70 dark:text-muted-foreground">
            <TextBlock title="Problem solved" value={project.problemSolved ?? project.description} />
            <TextBlock title="Why it matters" value={project.whyItMatters ?? "No extra rationale is present in the current Project Radar artifact."} />
          </CardContent>
        </Card>

        <Card className="border-[#dbe3dc] bg-white/85 dark:border-border dark:bg-card">
          <CardHeader>
            <CardTitle>Trend and adoption signals</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3 md:grid-cols-2">
            <Fact label="Trend score" value={formatScore(project.scores.trendScore)} />
            <Fact label="Star velocity" value={formatScore(project.scores.starVelocityScore)} />
            <Fact label="Activity score" value={formatScore(project.scores.activityScore)} />
            <Fact label="Freshness score" value={formatScore(project.scores.freshnessScore)} />
            <Fact label="Adoption score" value={formatScore(project.scores.adoptionScore)} />
            <Fact label="Evidence score" value={formatScore(project.scores.evidenceScore)} />
          </CardContent>
        </Card>

        <ReferenceList title="Related papers" emptyDescription="No related papers are present in the current artifact." items={project.relatedPapers ?? []} />
        <ReferenceList title="Related news" emptyDescription="No related news references are present in the current artifact." items={project.relatedNews ?? []} />
        <ReferenceList
          title="Community discussion"
          emptyDescription="No related community discussion is present in the current artifact."
          items={project.relatedCommunityTopics ?? []}
        />
        <EvidenceList refs={project.sourceRefs ?? []} />
      </section>

      <aside className="space-y-4">
        <Card className="border-[#dbe3dc] bg-white/85 dark:border-border dark:bg-card">
          <CardHeader>
            <CardTitle>Repository profile</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 text-sm">
            <Fact label="Repository" value={project.fullName} href={project.repoUrl} />
            <Fact label="Homepage" value={project.homepageUrl} href={project.homepageUrl} />
            <Fact label="Owner" value={project.owner} />
            <Fact label="Language" value={project.language} />
            <Fact label="License" value={project.license} />
            <Fact label="Open issues" value={formatOptionalNumber(project.openIssues)} />
            <Fact label="Created" value={formatDate(project.createdAt)} />
            <Fact label="Updated" value={formatDate(project.updatedAt ?? project.pushedAt ?? project.lastPushedAt)} />
            <Fact label="First seen" value={formatDate(project.firstSeenAt)} />
          </CardContent>
        </Card>

        <Card className="border-[#dbe3dc] bg-white/85 dark:border-border dark:bg-card">
          <CardHeader>
            <CardTitle>Topics</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2">
            {project.categoryRefs.map((category) => (
              <Badge key={category.category}>{category.label}</Badge>
            ))}
            {project.topics.map((topic) => (
              <Badge key={topic} variant="muted">
                {topic}
              </Badge>
            ))}
            {!project.categoryRefs.length && !project.topics.length ? <p className="text-sm text-muted-foreground">No topic labels are available.</p> : null}
          </CardContent>
        </Card>

        <Card className="border-[#dbe3dc] bg-white/85 dark:border-border dark:bg-card">
          <CardHeader>
            <CardTitle>Relation counts</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-2 text-sm">
            <RelationLine icon={<Layers3 className="size-4" />} label="Papers" value={project.relationCounts.papers} />
            <RelationLine icon={<Newspaper className="size-4" />} label="News" value={project.relationCounts.news} />
            <RelationLine icon={<MessageSquare className="size-4" />} label="Community" value={project.relationCounts.community} />
          </CardContent>
        </Card>
      </aside>
    </div>
  )
}

function Metric({ label, value, icon }: { label: string; value: string; icon?: ReactNode }) {
  return (
    <Card className="border-[#dbe3dc] bg-white/85 dark:border-border dark:bg-card">
      <CardContent className="p-4">
        <div className="flex items-center gap-2 text-xs text-[#334155]/55 dark:text-muted-foreground">
          {icon}
          <span className="font-mono uppercase">{label}</span>
        </div>
        <p className="mt-2 text-2xl font-semibold text-[#334155] dark:text-foreground">{value}</p>
      </CardContent>
    </Card>
  )
}

function TextBlock({ title, value }: { title: string; value: string }) {
  return (
    <div>
      <h2 className="text-sm font-semibold text-[#334155] dark:text-foreground">{title}</h2>
      <p className="mt-1">{value}</p>
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
  if (!items.length) {
    return <EmptyState title={`${title} empty`} description={emptyDescription} />
  }
  return (
    <Card className="border-[#dbe3dc] bg-white/85 dark:border-border dark:bg-card">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="divide-y divide-[#d8dfd8] p-0 dark:divide-border">
        {items.map((item) => (
          <a
            key={`${item.title}-${item.url ?? item.id ?? ""}`}
            href={item.url ?? "#"}
            target={item.url ? "_blank" : undefined}
            rel={item.url ? "noreferrer" : undefined}
            className="block px-4 py-3 text-sm hover:bg-[#f7f9f6] dark:hover:bg-background"
          >
            <span className="font-medium text-[#334155] dark:text-foreground">{item.title}</span>
            {"sourceName" in item && item.sourceName ? <span className="ml-2 text-muted-foreground">{item.sourceName}</span> : null}
          </a>
        ))}
      </CardContent>
    </Card>
  )
}

function EvidenceList({ refs }: { refs: EvidenceRef[] }) {
  if (!refs.length) {
    return <EmptyState title="Evidence empty" description="This project has no displayable evidence refs in the current artifact." />
  }
  return (
    <Card className="border-[#dbe3dc] bg-white/85 dark:border-border dark:bg-card">
      <CardHeader>
        <CardTitle>Evidence refs</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {refs.map((ref) => (
          <a
            key={`${ref.id ?? ""}-${ref.url ?? ref.sourceUrl ?? ""}`}
            href={ref.url ?? ref.sourceUrl ?? "#"}
            target={ref.url || ref.sourceUrl ? "_blank" : undefined}
            rel={ref.url || ref.sourceUrl ? "noreferrer" : undefined}
            className="block rounded-md border border-[#dbe3dc] p-3 text-sm hover:bg-[#f7f9f6] dark:border-border dark:hover:bg-background"
          >
            <span className="font-medium text-[#334155] dark:text-foreground">{ref.title ?? ref.sourceName ?? ref.url ?? ref.sourceUrl ?? "Evidence"}</span>
            <span className="mt-1 block text-xs text-muted-foreground">{ref.sourceType ?? ref.reliability ?? "source"}</span>
          </a>
        ))}
      </CardContent>
    </Card>
  )
}

function Fact({ label, value, href }: { label: string; value?: string; href?: string }) {
  if (!value) return null
  return (
    <div className="flex items-start justify-between gap-4 border-b border-[#d8dfd8] pb-3 last:border-0 last:pb-0 dark:border-border">
      <span className="text-muted-foreground">{label}</span>
      {href ? (
        <a href={href} target="_blank" rel="noreferrer" className="min-w-0 truncate text-right text-emerald-700 hover:underline">
          {value}
        </a>
      ) : (
        <span className="min-w-0 truncate text-right text-[#334155] dark:text-foreground">{value}</span>
      )}
    </div>
  )
}

function RelationLine({ icon, label, value }: { icon: ReactNode; label: string; value: number }) {
  return (
    <div className="flex items-center justify-between rounded-md border border-[#edf1ed] bg-[#f7f9f6] px-3 py-2 dark:border-border dark:bg-background">
      <span className="inline-flex items-center gap-2 text-muted-foreground">
        {icon}
        {label}
      </span>
      <span className="font-semibold text-[#334155] dark:text-foreground">{value}</span>
    </div>
  )
}

function formatCompactNumber(value: number | undefined): string {
  if (value === undefined) return "-"
  if (Math.abs(value) >= 1000) return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}K`
  return String(value)
}

function formatSignedNumber(value: number | undefined): string {
  if (value === undefined) return "-"
  if (value > 0) return `+${formatCompactNumber(value)}`
  return formatCompactNumber(value)
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

function labelize(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase())
}
