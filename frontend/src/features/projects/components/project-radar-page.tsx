"use client"

import Link from "next/link"
import { ArrowRight, GitBranch } from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { usePathname, useRouter, useSearchParams } from "next/navigation"
import { ErrorState } from "@/components/common/error-state"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { ProjectCard } from "@/features/projects/components/project-card"
import { ProjectDetailDrawer } from "@/features/projects/components/project-detail-drawer"
import { ProjectFilterPanel } from "@/features/projects/components/project-filter-panel"
import { ProjectToolbar } from "@/features/projects/components/project-toolbar"
import { formatCompactNumber, formatScore, formatSignedNumber } from "@/features/projects/components/project-format"
import { fetchProjects } from "@/lib/projects/api"
import {
  ProjectEmptyText,
  ProjectFilterChip,
  ProjectList,
  ProjectPage,
  ProjectPageBody,
  ProjectPageHeader,
  ProjectPanel,
  ProjectStat,
  projectGridClassName,
  projectListRowClassName,
} from "@/features/projects/components/project-page-layout"
import { dataStateBadgeVariant } from "@/features/projects/components/project-tone"
import type { ProjectItem, ProjectListParams, ProjectListResult, ProjectPeriod, ProjectSort } from "@/types/projects"

type ProjectClusterPreview = {
  id: string
  title: string
  items: ProjectItem[]
}

const SORT_TABS: Array<{ value: ProjectSort; label: string }> = [
  { value: "trending", label: "Trending" },
  { value: "newest", label: "Newest" },
  { value: "stars", label: "Stars" },
  { value: "activity", label: "Activity" },
]

const PERIOD_TABS: Array<{ value?: ProjectPeriod; label: string }> = [
  { value: undefined, label: "All" },
  { value: "daily", label: "Today" },
  { value: "weekly", label: "Week" },
  { value: "monthly", label: "Month" },
]

export function ProjectRadarPage() {
  const router = useRouter()
  const pathname = usePathname()
  const searchParams = useSearchParams()
  const [mobileFiltersOpen, setMobileFiltersOpen] = useState(false)
  const [data, setData] = useState<ProjectListResult | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const filters = useMemo(() => filtersFromSearchParams(searchParams), [searchParams])
  const selectedProject = searchParams.get("project")

  useEffect(() => {
    const controller = new AbortController()
    setIsLoading(true)
    setError(null)
    fetchProjects(filters, { signal: controller.signal })
      .then((result) => setData(result))
      .catch((requestError) => {
        if (!controller.signal.aborted) {
          setError(requestError instanceof Error ? requestError.message : "Project Radar request failed.")
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsLoading(false)
        }
      })
    return () => controller.abort()
  }, [filters])

  function setFilters(patch: Partial<ProjectListParams>) {
    const next = { ...filters, ...patch }
    router.replace(urlFor(next, selectedProject), { scroll: false })
  }

  function closeDrawer() {
    router.replace(urlFor(filters, null), { scroll: false })
  }

  function projectHref(slug: string) {
    return urlFor(filters, slug)
  }

  function urlFor(nextFilters: ProjectListParams, projectSlug?: string | null) {
    const query = filtersToSearchParams(nextFilters, projectSlug).toString()
    return query ? `${pathname}?${query}` : pathname
  }

  if (isLoading && !data) {
    return <PageSkeleton />
  }

  if (error && !data) {
    return <ErrorState title="Project Radar failed to load" message={error} onRetry={() => setFilters({})} />
  }

  if (!data) {
    return <ProjectEmptyText title="Project Radar is empty" description="No project_radar data was returned by the current data source." />
  }

  const allFiltered = data.allFiltered ?? data.items
  const totalPages = Math.max(1, Math.ceil(data.page.total / data.page.pageSize))
  const topProject = pickTopProject(allFiltered)
  const streamItems = topProject ? data.items.filter((project) => project.id !== topProject.id) : data.items
  const clusters = buildClusters(allFiltered)
  const closeHref = urlFor(filters, null)

  return (
    <ProjectPage>
      <ProjectPageHeader
        eyebrow="Agora Hub / Project Radar"
        title="Project Radar Board"
        icon={<GitBranch className="size-4" strokeWidth={1.8} />}
        subtitle="Track AI open-source projects, engineering practice, repository momentum, adoption signals, and cross-board evidence."
      >
        <div className="flex flex-wrap gap-1.5">
          <ProjectStat label="Visible projects" value={allFiltered.length} />
          <ProjectStat label="Categories" value={data.options.categories.length} />
          <ProjectStat label="Languages" value={data.options.languages.length} />
          <ProjectStat label="Source state" value={data.source} />
        </div>
        <ProjectToolbar filters={filters} onChange={setFilters} onToggleFilters={() => setMobileFiltersOpen((value) => !value)} />
        <FilterBar filters={filters} onChange={setFilters} />
      </ProjectPageHeader>

      <div className="grid min-w-0 xl:grid-cols-[15rem_minmax(0,1fr)]">
        <aside className="hidden border-e border-border px-2 py-5 xl:block">
          <ProjectFilterPanel filters={filters} options={data.options} onChange={setFilters} />
        </aside>

        <ProjectPageBody className="space-y-5">
          {data.dataState !== "ready" || data.notices.length ? <DegradedBanner data={data} /> : null}

          <div className={mobileFiltersOpen ? "block rounded-xl border border-border p-3 xl:hidden" : "hidden"}>
            <ProjectFilterPanel filters={filters} options={data.options} onChange={setFilters} />
          </div>

          <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_20rem]">
            {topProject ? <TrendingProjectCard project={topProject} href={projectHref(topProject.slug)} /> : null}
            <CurrentLeadCard project={topProject} href={topProject ? projectHref(topProject.slug) : undefined} dataState={data.dataState} />
          </div>
          {clusters.length ? <ClusterRail clusters={clusters} projectHref={projectHref} /> : null}

          <div className="flex items-center justify-between gap-3 text-label text-muted-foreground">
            <span>
              Showing {data.items.length} of {data.page.total} matching projects
            </span>
            <span>
              Page {data.page.page} of {totalPages}
            </span>
          </div>

          {isLoading ? <p className="text-body2 text-muted-foreground" role="status">Refreshing project stream...</p> : null}
          {error ? <ErrorState title="Project stream refresh failed" message={error} /> : null}

          {streamItems.length ? (
            <div className={projectGridClassName}>
              {streamItems.map((project) => (
                <ProjectCard key={project.id} project={project} detailHref={projectHref(project.slug)} />
              ))}
            </div>
          ) : (
            <ProjectEmptyText
              title={data.allItems.length ? "No projects match these filters" : "Project Radar is empty"}
              description={
                data.allItems.length
                  ? "Try a different query, topic, language, maturity, or period."
                  : "No public GitHub project records are available from backend or local Project Radar artifacts."
              }
              action={
                data.allItems.length ? (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      setFilters({
                        q: undefined,
                        category: undefined,
                        topic: undefined,
                        source: undefined,
                        language: undefined,
                        maturity: undefined,
                        period: undefined,
                        page: 1,
                        cursor: undefined,
                      })
                    }
                  >
                    Clear filters
                  </Button>
                ) : null
              }
            />
          )}

          <div className="flex items-center justify-between gap-3">
            <Button type="button" variant="outline" size="sm" disabled={data.page.page <= 1} onClick={() => setFilters({ page: data.page.page - 1, cursor: undefined })}>
              Previous page
            </Button>
            <Button type="button" variant="outline" size="sm" disabled={!data.page.hasNext} onClick={() => setFilters({ page: data.page.page + 1, cursor: undefined })}>
              Next page
            </Button>
          </div>
        </ProjectPageBody>
      </div>

      <ProjectDetailDrawer projectSlug={selectedProject} open={Boolean(selectedProject)} closeHref={closeHref} onOpenChange={(open) => !open && closeDrawer()} />
    </ProjectPage>
  )
}

function FilterBar({ filters, onChange }: { filters: ProjectListParams; onChange: (patch: Partial<ProjectListParams>) => void }) {
  return (
    <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Project period">
        {PERIOD_TABS.map((period) => (
          <ProjectFilterChip
            key={period.label}
            active={(filters.period ?? "") === (period.value ?? "")}
            onClick={() => onChange({ period: period.value, page: 1, cursor: undefined })}
          >
            {period.label}
          </ProjectFilterChip>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Project sort">
        {SORT_TABS.map((sort) => (
          <ProjectFilterChip
            key={sort.value}
            active={(filters.sort ?? "trending") === sort.value}
            onClick={() => onChange({ sort: sort.value, page: 1, cursor: undefined })}
          >
            {sort.label}
          </ProjectFilterChip>
        ))}
      </div>
    </div>
  )
}

function CurrentLeadCard({ project, href, dataState }: { project?: ProjectItem; href?: string; dataState: string }) {
  return (
    <ProjectPanel eyebrow="Current lead">
      {project && href ? (
        <div className="space-y-3">
          <div className="flex items-start gap-3">
            <span className="grid size-9 shrink-0 place-items-center rounded-[10px] bg-accent text-accent-foreground" aria-hidden="true">
              <GitBranch className="size-4" strokeWidth={1.8} />
            </span>
            <div className="min-w-0">
              <h2 className="line-clamp-2 text-[15px] font-semibold text-foreground">{project.name}</h2>
              <p className="mt-0.5 truncate text-label text-muted-foreground">{project.fullName}</p>
            </div>
          </div>
          <Button asChild variant="outline" size="sm">
            <Link href={href}>
              View detail
              <ArrowRight strokeWidth={1.8} />
            </Link>
          </Button>
        </div>
      ) : (
        <p className="text-body2 text-muted-foreground">
          No real Project Radar output is available from backend or local artifacts yet. State: {dataState}.
        </p>
      )}
    </ProjectPanel>
  )
}

function TrendingProjectCard({ project, href }: { project: ProjectItem; href: string }) {
  return (
    <article className="min-w-0 rounded-2xl border border-border bg-card p-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <p className="text-label text-muted-foreground">Trending Project</p>
          <h2 className="mt-1 break-words text-heading3 font-semibold text-foreground">
            <Link href={href} className="rounded-sm outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50">
              {project.fullName}
            </Link>
          </h2>
          <p className="mt-1.5 max-w-4xl break-words text-body2 text-muted-foreground">{project.whyItMatters ?? project.description}</p>
        </div>
        <Button asChild variant="outline" size="sm">
          <a href={project.repoUrl} target="_blank" rel="noreferrer">
            Open repo
            <ArrowRight strokeWidth={1.8} />
          </a>
        </Button>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <Badge variant="info">Trend {formatScore(project.scores.trendScore ?? project.projectMomentum)}</Badge>
        <Badge variant="muted">Stars {formatCompactNumber(project.stars)}</Badge>
        <Badge variant="muted">Delta {formatSignedNumber(project.starGrowth7d ?? project.starGrowth24h)}</Badge>
        <Badge variant="muted">{project.relationCounts.papers} papers</Badge>
        <Badge variant="muted">{project.relationCounts.news} news</Badge>
        <Badge variant="muted">{project.relationCounts.community} community</Badge>
      </div>
    </article>
  )
}

function ClusterRail({ clusters, projectHref }: { clusters: ProjectClusterPreview[]; projectHref: (slug: string) => string }) {
  return (
    <section className="grid gap-3 lg:grid-cols-2">
      {clusters.slice(0, 2).map((cluster) => (
        <section key={cluster.id} className="min-w-0 space-y-2">
          <div>
            <p className="text-label text-muted-foreground">Repository Cluster</p>
            <h3 className="text-body1 font-semibold text-foreground">{cluster.title}</h3>
          </div>
          <ProjectList>
            {cluster.items.slice(0, 3).map((project) => (
              <Link key={project.id} href={projectHref(project.slug)} className={projectListRowClassName}>
                <span className="font-medium text-foreground">{project.fullName}</span>
                <span className="mt-0.5 line-clamp-1 block text-muted-foreground">{project.description}</span>
              </Link>
            ))}
          </ProjectList>
        </section>
      ))}
    </section>
  )
}

function DegradedBanner({ data }: { data: ProjectListResult }) {
  return (
    <div className="flex flex-col gap-2 rounded-xl border border-warning/30 bg-warning/10 px-4 py-3 sm:flex-row sm:items-center sm:justify-between" role="status">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={dataStateBadgeVariant(data.dataState)}>{data.dataState}</Badge>
          <p className="text-body2 font-medium text-foreground">Project Radar is using only real backend or local artifact data.</p>
        </div>
        <p className="mt-1 text-body2 text-muted-foreground">No bundled mock projects will be substituted when real data is missing.</p>
      </div>
      {data.notices.length ? <p className="break-words text-label text-muted-foreground sm:max-w-sm">{data.notices[data.notices.length - 1]}</p> : null}
    </div>
  )
}

function pickTopProject(items: ProjectItem[]) {
  return [...items].sort((left, right) => topProjectScore(right) - topProjectScore(left))[0]
}

function topProjectScore(project: ProjectItem) {
  return (
    (project.scores.trendScore ?? project.projectMomentum ?? 0) * 0.45 +
    (project.scores.starVelocityScore ?? project.starGrowth7d ?? project.starGrowth24h ?? 0) * 0.25 +
    (project.scores.activityScore ?? 0) * 0.15 +
    (project.scores.evidenceScore ?? 0) * 0.15
  )
}

function buildClusters(items: ProjectItem[]): ProjectClusterPreview[] {
  const groups = new Map<string, ProjectItem[]>()
  for (const item of items) {
    const key = item.categories[0] ?? item.topics[0]
    if (!key) continue
    groups.set(key, [...(groups.get(key) ?? []), item])
  }
  return [...groups.entries()]
    .filter(([, groupItems]) => groupItems.length > 1)
    .map(([id, groupItems]) => ({
      id,
      title: id.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase()),
      items: groupItems,
    }))
}

function filtersFromSearchParams(searchParams: Pick<URLSearchParams, "get">): ProjectListParams {
  return {
    q: searchParams.get("q") ?? undefined,
    category: searchParams.get("category") as ProjectListParams["category"],
    topic: searchParams.get("topic") ?? undefined,
    sort: searchParams.get("sort") as ProjectListParams["sort"],
    source: searchParams.get("source") as ProjectListParams["source"],
    language: searchParams.get("language") as ProjectListParams["language"],
    license: searchParams.get("license") as ProjectListParams["license"],
    maturity: searchParams.get("maturity") as ProjectListParams["maturity"],
    period: searchParams.get("period") as ProjectListParams["period"],
    page: numberParam(searchParams.get("page")),
    pageSize: numberParam(searchParams.get("pageSize")),
    limit: numberParam(searchParams.get("limit")),
    cursor: searchParams.get("cursor") ?? undefined,
  }
}

function filtersToSearchParams(filters: ProjectListParams, projectSlug?: string | null): URLSearchParams {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (
      value === undefined ||
      value === null ||
      value === "" ||
      (key === "page" && value === 1) ||
      (key === "sort" && value === "trending") ||
      (key === "period" && value === "all")
    ) {
      continue
    }
    params.set(key, String(value))
  }
  if (projectSlug) {
    params.set("project", projectSlug)
  }
  return params
}

function numberParam(value: string | null): number | undefined {
  if (!value) return undefined
  const number = Number(value)
  return Number.isFinite(number) ? number : undefined
}
