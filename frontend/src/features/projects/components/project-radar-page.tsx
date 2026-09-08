"use client"

import Link from "next/link"
import { ArrowRight, GitBranch } from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { usePathname, useRouter, useSearchParams } from "next/navigation"
import { EmptyState } from "@/components/common/empty-state"
import { ErrorState } from "@/components/common/error-state"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { ProjectCard } from "@/features/projects/components/project-card"
import { ProjectDetailDrawer } from "@/features/projects/components/project-detail-drawer"
import { ProjectFilterPanel } from "@/features/projects/components/project-filter-panel"
import { ProjectToolbar } from "@/features/projects/components/project-toolbar"
import { fetchProjects } from "@/lib/projects/api"
import { cn } from "@/lib/utils"
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
    return <EmptyState title="Project Radar is empty" description="No project_radar data was returned by the current data source." />
  }

  const allFiltered = data.allFiltered ?? data.items
  const totalPages = Math.max(1, Math.ceil(data.page.total / data.page.pageSize))
  const topProject = pickTopProject(allFiltered)
  const streamItems = topProject ? data.items.filter((project) => project.id !== topProject.id) : data.items
  const clusters = buildClusters(allFiltered)
  const closeHref = urlFor(filters, null)

  return (
    <main className="space-y-8 font-papers-research">
      <section className="grid gap-8 py-8 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-end">
        <div className="min-w-0">
          <p className="mb-5 text-xs font-medium uppercase tracking-[0.18em] text-[#334155]/55 dark:text-muted-foreground">
            Agora Hub / Project Radar
          </p>
          <h1 className="max-w-5xl break-keep text-5xl font-black leading-none tracking-normal text-[#334155] sm:text-6xl lg:text-7xl dark:text-foreground">
            Project Radar{" "}
            <span className="bg-gradient-to-r from-emerald-600 via-teal-600 to-blue-600 bg-clip-text text-transparent">
              Board
            </span>
          </h1>
          <p className="mt-6 max-w-3xl text-base leading-7 text-[#334155]/70 dark:text-muted-foreground">
            Track AI open-source projects, engineering practice, repository momentum, adoption signals, and cross-board evidence.
          </p>
          <div className="mt-7 flex flex-wrap gap-2">
            <HeroPill label="Visible projects" value={allFiltered.length} />
            <HeroPill label="Categories" value={data.options.categories.length} />
            <HeroPill label="Languages" value={data.options.languages.length} />
            <HeroPill label="Source state" value={data.source} />
          </div>
        </div>

        <CurrentLeadCard project={topProject} href={topProject ? projectHref(topProject.slug) : undefined} dataState={data.dataState} />
      </section>

      <section className="grid gap-8 xl:grid-cols-[16rem_minmax(0,1fr)]">
        <aside className="hidden xl:block">
          <ProjectFilterPanel filters={filters} options={data.options} onChange={setFilters} />
        </aside>

        <section className="min-w-0 space-y-5">
          <FilterBar filters={filters} onChange={setFilters} />
          <ProjectToolbar filters={filters} onChange={setFilters} onToggleFilters={() => setMobileFiltersOpen((value) => !value)} />

          {data.dataState !== "ready" || data.notices.length ? <DegradedBanner data={data} /> : null}

          <div className={mobileFiltersOpen ? "block xl:hidden" : "hidden"}>
            <ProjectFilterPanel filters={filters} options={data.options} onChange={setFilters} />
          </div>

          {topProject ? <TrendingProjectCard project={topProject} href={projectHref(topProject.slug)} /> : null}
          {clusters.length ? <ClusterRail clusters={clusters} projectHref={projectHref} /> : null}

          <div className="flex items-center justify-between gap-3 text-xs text-[#334155]/55 dark:text-muted-foreground">
            <span>
              Showing {data.items.length} of {data.page.total} matching projects
            </span>
            <span>
              Page {data.page.page} of {totalPages}
            </span>
          </div>

          {isLoading ? <p className="text-sm text-muted-foreground">Refreshing project stream...</p> : null}
          {error ? <ErrorState title="Project stream refresh failed" message={error} /> : null}

          {streamItems.length ? (
            <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-3">
              {streamItems.map((project) => (
                <ProjectCard key={project.id} project={project} detailHref={projectHref(project.slug)} />
              ))}
            </div>
          ) : (
            <EmptyState
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
            <Button type="button" variant="outline" disabled={data.page.page <= 1} onClick={() => setFilters({ page: data.page.page - 1, cursor: undefined })}>
              Previous page
            </Button>
            <Button type="button" variant="outline" disabled={!data.page.hasNext} onClick={() => setFilters({ page: data.page.page + 1, cursor: undefined })}>
              Next page
            </Button>
          </div>
        </section>
      </section>

      <ProjectDetailDrawer projectSlug={selectedProject} open={Boolean(selectedProject)} closeHref={closeHref} onOpenChange={(open) => !open && closeDrawer()} />
    </main>
  )
}

function FilterBar({ filters, onChange }: { filters: ProjectListParams; onChange: (patch: Partial<ProjectListParams>) => void }) {
  return (
    <div className="flex flex-col gap-4 border-y border-[#d7dfd8] py-4 md:flex-row md:items-center md:justify-between dark:border-border">
      <div className="flex flex-wrap items-center gap-2" aria-label="Project period">
        {PERIOD_TABS.map((period) => (
          <button
            key={period.label}
            type="button"
            onClick={() => onChange({ period: period.value, page: 1, cursor: undefined })}
            className={cn(
              "h-8 rounded-full border px-3 text-sm font-semibold transition-colors",
              (filters.period ?? "") === (period.value ?? "")
                ? "border-[#315d8a] bg-[#315d8a] text-white"
                : "border-[#d7dfd8] bg-white text-[#334155]/65 hover:border-[#315d8a]/40 hover:text-[#334155] dark:border-border dark:bg-card dark:text-muted-foreground"
            )}
          >
            {period.label}
          </button>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2" aria-label="Project sort">
        {SORT_TABS.map((sort) => (
          <button
            key={sort.value}
            type="button"
            onClick={() => onChange({ sort: sort.value, page: 1, cursor: undefined })}
            className={cn(
              "h-8 rounded-full border px-3 text-sm font-semibold transition-colors",
              (filters.sort ?? "trending") === sort.value
                ? "border-emerald-700 bg-emerald-700 text-white"
                : "border-[#d7dfd8] bg-white text-[#334155]/65 hover:border-emerald-700/40 hover:text-[#334155] dark:border-border dark:bg-card dark:text-muted-foreground"
            )}
          >
            {sort.label}
          </button>
        ))}
      </div>
    </div>
  )
}

function CurrentLeadCard({ project, href, dataState }: { project?: ProjectItem; href?: string; dataState: string }) {
  return (
    <div className="rounded-md border border-[#dbe3dc] bg-white/90 p-5 shadow-[0_24px_60px_rgba(15,23,42,0.10)] dark:border-border dark:bg-card">
      <p className="text-xs font-semibold uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">Current lead</p>
      {project && href ? (
        <div className="mt-4 space-y-4">
          <div className="flex items-start gap-3">
            <span className="flex size-10 shrink-0 items-center justify-center rounded-md bg-[#0f172a] text-white">
              <GitBranch className="size-5" />
            </span>
            <div className="min-w-0">
              <h2 className="line-clamp-2 text-lg font-semibold text-[#334155] dark:text-foreground">{project.name}</h2>
              <p className="mt-1 truncate text-sm text-[#334155]/60 dark:text-muted-foreground">{project.fullName}</p>
            </div>
          </div>
          <Link href={href} className="inline-flex items-center gap-2 text-sm font-semibold text-emerald-700 hover:text-emerald-800">
            View detail
            <ArrowRight className="size-4" />
          </Link>
        </div>
      ) : (
        <p className="mt-4 text-sm leading-6 text-[#334155]/60 dark:text-muted-foreground">
          No real Project Radar output is available from backend or local artifacts yet. State: {dataState}.
        </p>
      )}
    </div>
  )
}

function TrendingProjectCard({ project, href }: { project: ProjectItem; href: string }) {
  return (
    <article className="rounded-md border border-[#dbe3dc] bg-white/90 p-5 shadow-sm dark:border-border dark:bg-card">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#334155]/55 dark:text-muted-foreground">Trending Project</p>
          <Link href={href}>
            <h2 className="mt-2 max-w-4xl text-2xl font-semibold tracking-normal text-[#334155] hover:text-emerald-700 dark:text-foreground">
              {project.fullName}
            </h2>
          </Link>
          <p className="mt-3 max-w-4xl text-sm leading-6 text-[#334155]/68 dark:text-muted-foreground">
            {project.whyItMatters ?? project.description}
          </p>
        </div>
        <a
          href={project.repoUrl}
          target="_blank"
          rel="noreferrer"
          className="inline-flex shrink-0 items-center gap-2 rounded-md border border-[#dbe3dc] bg-white px-3 py-2 text-sm text-[#334155] hover:bg-[#f7f9f6] dark:border-border dark:bg-background dark:text-foreground"
        >
          Open repo
          <ArrowRight className="size-4" />
        </a>
      </div>
      <div className="mt-5 flex flex-wrap items-center gap-2">
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
        <article key={cluster.id} className="rounded-md border border-[#dbe3dc] bg-white/75 p-4 dark:border-border dark:bg-card">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#334155]/55 dark:text-muted-foreground">Repository Cluster</p>
          <h3 className="mt-2 text-base font-semibold text-[#334155] dark:text-foreground">{cluster.title}</h3>
          <div className="mt-3 space-y-2">
            {cluster.items.slice(0, 3).map((project) => (
              <Link key={project.id} href={projectHref(project.slug)} className="block text-sm text-[#334155]/70 hover:text-emerald-700 dark:text-muted-foreground">
                {project.fullName}: {project.description}
              </Link>
            ))}
          </div>
        </article>
      ))}
    </section>
  )
}

function DegradedBanner({ data }: { data: ProjectListResult }) {
  return (
    <Card className="flex flex-col gap-2 border-amber-200 bg-amber-50 p-4 text-amber-950 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <Badge variant={data.dataState === "empty" ? "muted" : "warning"}>{data.dataState}</Badge>
          <p className="text-sm font-medium">Project Radar is using only real backend or local artifact data.</p>
        </div>
        <p className="mt-2 text-sm leading-6">No bundled mock projects will be substituted when real data is missing.</p>
      </div>
      {data.notices.length ? <p className="text-xs sm:max-w-sm">{data.notices[data.notices.length - 1]}</p> : null}
    </Card>
  )
}

function HeroPill({ label, value }: { label: string; value: string | number }) {
  return (
    <span className="inline-flex items-center gap-2 rounded-full border border-[#dbe5dd] bg-white/90 px-3 py-1 text-xs text-[#334155]/70 shadow-sm dark:border-border dark:bg-card dark:text-muted-foreground">
      <span className="font-medium text-[#334155] dark:text-foreground">{label}</span>
      {value}
    </span>
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

function formatScore(value: number | undefined): string {
  if (value === undefined) return "-"
  return value <= 1 ? `${Math.round(value * 100)}%` : value.toFixed(1)
}
