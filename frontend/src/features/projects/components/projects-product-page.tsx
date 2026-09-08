"use client"

import { ResearchMaterialContext } from "@/components/research/research-material-context"

import Link from "next/link"
import { AlertCircle, ArrowRight, Binoculars, Boxes, Check, ChevronRight, CircleHelp, Clipboard, ExternalLink, FlaskConical, Flame, FolderKanban, GitBranch, Info, Loader2, Search, Sparkles, Star } from "lucide-react"
import type { ComponentType } from "react"
import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { usePathname, useRouter, useSearchParams } from "next/navigation"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  addProjectWatchlistItem,
  answerProjectLabQuestion,
  fetchProjectProductSection,
  fetchProjectsHome,
  generateProjectLabSolution,
  explainProjectLabNode,
  ProjectsApiError,
  recordProjectInteraction,
  projectProductSection,
  startProjectLabSession,
} from "@/lib/projects/api"
import { formatScore, labelize } from "@/features/projects/components/project-format"
import { ProjectProductCard } from "@/features/projects/components/project-product-card"
import { ProjectDegradedNotice, ProjectEmptyState, ProjectErrorState, ProjectLoadingState, ProjectSourceLine } from "@/features/projects/components/project-product-state"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { PapersHero } from "@/components/papers/papers-hero"
import { PapersMicrobar } from "@/components/papers/papers-microbar"
import { presentLabWorkflow, labQuestionAnswered, labSolutionValue } from "@/features/projects/components/lab-workflow"
import { useI18n } from "@/lib/i18n/use-i18n"
import type {
  ProjectCategoryAlias,
  ProjectProductRoute,
  ProjectProductSection,
  ProjectsApiCaseResult,
  ProjectsApiCollectionResult,
  ProjectsApiHomeResult,
  ProjectsApiListResult,
  ProjectsApiMeta,
  ProjectsApiProject,
  ProjectsApiToolResult,
  ProjectsApiWatchlistItem,
  ProjectsApiWatchlistResult,
  ProjectsLabSession,
  ProjectsLabSolutionResult,
} from "@/types/projects"

type ProjectsProductPageProps = {
  route: ProjectProductRoute
}

type UnknownRecord = Record<string, unknown>

const routeIcons: Record<ProjectProductRoute, ComponentType<{ className?: string }>> = {
  home: GitBranch,
  hot: Flame,
  rising: Sparkles,
  tools: Boxes,
  cases: FolderKanban,
  lab: FlaskConical,
  collections: Binoculars,
  watchlist: Star,
}

export function ProjectsProductPage({ route }: ProjectsProductPageProps) {
  const section = useMemo(() => projectProductSection(route), [route])
  const searchParams = useSearchParams()
  const router = useRouter()
  const pathname = usePathname()
  const urlCategory = projectCategoryAlias(searchParams?.get("category") ?? null)
  const initialQuery = searchParams?.get("q") ?? searchParams?.get("question") ?? ""
  const [query, setQuery] = useState(initialQuery)
  const sourceQuestion = searchParams?.get("question") ?? ""
  useEffect(() => {
    setQuery(initialQuery)
  }, [initialQuery])
  function submitQuery() {
    const params = new URLSearchParams(searchParams?.toString())
    if (query.trim() || sourceQuestion) params.set("q", query.trim())
    else params.delete("q")
    router.replace(`${pathname || "/projects"}${params.size ? `?${params}` : ""}`, { scroll: false })
  }
  const queryParams = useMemo(
    () => ({ q: initialQuery.trim() || undefined, limit: route === "home" ? 6 : 18, category: urlCategory }),
    [initialQuery, route, urlCategory]
  )

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["projects", "product-v1", route, queryParams],
    queryFn: () =>
      route === "home" && !queryParams.q
        ? fetchProjectsHome({ limit: queryParams.limit })
        : route === "home"
          ? fetchProjectProductSection("hot", { params: { ...queryParams, limit: 18 } })
          : fetchProjectProductSection(route, { params: queryParams }),
  })

  if (isLoading) return route === "lab" ? <LabWorkspaceSkeleton /> : <ProjectLoadingState title={route === "home" ? "Loading Projects home" : `Loading ${section.title}`} />
  if (isError) return <ProjectErrorState message={error instanceof Error ? error.message : undefined} onRetry={() => refetch()} />
  if (!data) return <ProjectEmptyState />

  if (route === "lab") {
    return <LabRoute data={data} section={section} isFetching={isFetching} />
  }

  const meta = getMeta(data)
  return (
    <main className="space-y-9 font-papers-research">
      <ProjectHero section={section} route={route} data={data} query={query} onQueryChange={setQuery} onSearch={submitQuery} isFetching={isFetching} sourceQuestion={sourceQuestion} />
      {meta ? <ProjectDegradedNotice meta={meta} /> : null}
      <RouteContent route={route} data={data} onRefresh={() => void refetch()} />
    </main>
  )
}

function LabRoute({ data, section, isFetching }: { data: ProductData; section: ProjectProductSection; isFetching: boolean }) {
  const { t, locale } = useI18n()
  const meta = getMeta(data)

  return (
    <main className="space-y-5 font-papers-research">
      <PapersMicrobar items={[{ label: section.title }]} meta={t("projects.lab.headerMeta")} locale={locale} />
      <PapersHero
        variant="editorial"
        eyebrow={t("projects.lab.headerEyebrow")}
        title={section.title}
        subtitle={t("projects.lab.headerDescription")}
        stats={[
          { label: t("projects.lab.statsCases"), value: caseCount(data) },
          { label: t("projects.lab.statsProjects"), value: projectCount(data) },
          { label: t("projects.lab.statsState"), value: meta?.data_state ?? "empty" },
        ]}
        aside={
          <div className="flex min-h-9 items-center justify-between gap-3 rounded-xl border border-[#dfe5df] bg-white/80 px-3 py-2.5 shadow-sm dark:border-border dark:bg-card">
            {meta ? <ProjectSourceLine meta={meta} /> : null}
            {isFetching ? <span className="inline-flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground"><Loader2 className="size-3.5 animate-spin" /> {t("projects.lab.refreshing")}</span> : null}
          </div>
        }
      />
      <LabView data={data} />
    </main>
  )
}

function projectCategoryAlias(value: string | null): ProjectCategoryAlias | undefined {
  if (!value) {
    return undefined
  }
  const aliases: ProjectCategoryAlias[] = [
    "agent_framework",
    "rag",
    "llm_infra",
    "inference",
    "evaluation",
    "coding",
    "multimodal",
    "data",
    "memory",
    "workflow",
    "agent",
    "devtool",
    "framework",
    "infra",
    "llm",
    "dataset",
  ]
  return aliases.includes(value as ProjectCategoryAlias) ? (value as ProjectCategoryAlias) : undefined
}

function ProjectHero({
  section,
  route,
  data,
  query,
  onQueryChange,
  onSearch,
  isFetching,
  sourceQuestion,
}: {
  section: ProjectProductSection
  route: ProjectProductRoute
  data: ProductData
  query: string
  onQueryChange: (value: string) => void
  onSearch: () => void
  isFetching: boolean
  sourceQuestion: string
}) {
  const Icon = routeIcons[route]
  const meta = getMeta(data)
  const lead = firstProject(data)

  return (
    <section className="grid gap-8 py-8 lg:grid-cols-[minmax(0,1fr)_24rem] lg:items-end">
      <div className="min-w-0">
        {sourceQuestion ? <div className="mb-4 rounded-lg border border-[#e7dff1] bg-[#faf7ff] px-3 py-2 text-sm text-[#695d7d]">来自首页的问题：<span className="font-medium text-[#382758]">{sourceQuestion}</span></div> : null}
        <ResearchMaterialContext />
        <div className="mb-5 flex flex-wrap items-center gap-2">
          <Badge variant="info">Projects API v1</Badge>
          <Badge variant="muted">Real Project Radar data</Badge>
          {isFetching ? <Badge variant="muted">Refreshing</Badge> : null}
        </div>
        <h1 className="max-w-5xl text-5xl font-black leading-none tracking-normal text-[#202124] sm:text-6xl lg:text-7xl dark:text-foreground">
          {section.title}
        </h1>
        <p className="mt-6 max-w-3xl text-base leading-7 text-[#4b5563] dark:text-muted-foreground">{section.description}</p>
        <div className="mt-7 flex flex-wrap gap-2">
          <HeroPill label="Projects" value={projectCount(data)} />
          <HeroPill label="Cases" value={caseCount(data)} />
          <HeroPill label="Collections" value={collectionCount(data)} />
          <HeroPill label="State" value={meta?.data_state ?? "empty"} />
        </div>
        <form className="mt-6 max-w-2xl" onSubmit={event => { event.preventDefault(); onSearch() }}>
          <label className="sr-only" htmlFor="projects-product-search">
            Search projects
          </label>
          <div className="relative flex items-center gap-2">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id="projects-product-search"
              value={query}
              onChange={(event) => onQueryChange(event.target.value)}
              onKeyDown={event => { if (event.key === "Enter" && (event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault() }}
              className="h-11 rounded-md bg-white pl-9 dark:bg-card"
              placeholder="Search real projects, tools, cases, or module themes"
            />
            <Button type="submit" className="h-11 shrink-0">搜索</Button>
          </div>
        </form>
      </div>

      <aside className="rounded-md border border-[#d8dee7] bg-white p-5 shadow-sm dark:border-border dark:bg-card">
        <div className="flex items-start gap-3">
          <span className="flex size-11 shrink-0 items-center justify-center rounded-md bg-[#0f172a] text-white dark:bg-primary">
            <Icon className="size-5" />
          </span>
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase text-[#667085] dark:text-muted-foreground">Current focus</p>
            {lead ? (
              <>
                <h2 className="mt-2 line-clamp-2 text-lg font-semibold text-[#202124] dark:text-foreground">{lead.name}</h2>
                <p className="mt-2 text-sm leading-6 text-[#4b5563] dark:text-muted-foreground">
                  Score {formatScore(lead.hot_score ?? lead.rising_score ?? undefined)} / Sources {lead.source_count}
                </p>
                <Button asChild className="mt-4" size="sm">
                  <Link href={`/projects/${encodeURIComponent(lead.slug)}`}>
                    View project
                    <ArrowRight className="size-4" />
                  </Link>
                </Button>
              </>
            ) : (
              <p className="mt-2 text-sm leading-6 text-[#4b5563] dark:text-muted-foreground">No real Project Radar output is available yet.</p>
            )}
          </div>
        </div>
      </aside>
    </section>
  )
}

function RouteContent({ route, data, onRefresh }: { route: ProjectProductRoute; data: ProductData; onRefresh: () => void }) {
  if (route === "home") {
    return "items" in data && "page" in data
      ? <ProjectGrid projects={data.items} meta={data.meta} />
      : <ProjectsHome data={data as ProjectsApiHomeResult} />
  }
  if (route === "tools") return <ToolsView data={data as ProjectsApiToolResult} />
  if (route === "cases") return <CasesView data={data as ProjectsApiCaseResult} />
  if (route === "collections") return <CollectionsView data={data as ProjectsApiCollectionResult} />
  if (route === "watchlist") return <WatchlistView data={data as ProjectsApiWatchlistResult} onChanged={onRefresh} />
  if (route === "lab") return <LabView data={data} />
  return <ProjectGrid projects={(data as ProjectsApiListResult).items} meta={(data as ProjectsApiListResult).meta} />
}

function ProjectsHome({ data }: { data: ProjectsApiHomeResult }) {
  const projects = uniqueProjects([...data.hot, ...data.rising, ...data.tools])
  if (!projects.length && !data.cases.length && !data.collections.length) return <ProjectEmptyState />
  return (
    <section className="space-y-8">
      <SectionHeader title="Hot and rising" href="/projects/hot" />
      <ProjectGrid projects={projects.slice(0, 6)} meta={data.meta} />
      <SectionHeader title="Cases and collections" href="/projects/cases" />
      <div className="grid gap-4 lg:grid-cols-2">
        {data.cases.slice(0, 4).map((item) => (
          <SimplePanel key={item.id} title={String(item.title)} subtitle={String(item.module_type)} body={String(item.design_summary ?? item.problem ?? "")} />
        ))}
        {data.collections.slice(0, 4).map((item) => (
          <SimplePanel key={item.id} title={item.title} subtitle={`${item.item_count ?? 0} items`} body={item.description} href={`/projects/collections`} />
        ))}
      </div>
    </section>
  )
}

function ToolsView({ data }: { data: ProjectsApiToolResult }) {
  const [hasApi, setHasApi] = useState(false)
  const [hasCli, setHasCli] = useState(false)
  const [hasPython, setHasPython] = useState(false)
  const [hasDocker, setHasDocker] = useState(false)
  const [difficulty, setDifficulty] = useState<string>("")

  const filtered = data.tools.filter((tool) => {
    if (hasApi && !tool.profile.has_api) return false
    if (hasCli && !tool.profile.has_cli) return false
    if (hasPython && !tool.profile.has_python_sdk) return false
    if (hasDocker && !tool.profile.has_docker) return false
    if (difficulty && tool.profile.integration_difficulty !== difficulty) return false
    return true
  })

  if (!data.tools.length) return <ProjectEmptyState title="No Real-Derived Tools" />
  return (
    <section className="grid gap-4 lg:grid-cols-[minmax(0,16rem)_1fr]">
      <div className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card h-fit">
        <h3 className="text-sm font-semibold text-[#202124] dark:text-foreground mb-3">Filters</h3>
        <div className="space-y-3">
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={hasApi} onChange={(e) => setHasApi(e.target.checked)} className="rounded" />
            <span className="text-sm text-[#4b5563] dark:text-muted-foreground">Has API</span>
          </label>
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={hasCli} onChange={(e) => setHasCli(e.target.checked)} className="rounded" />
            <span className="text-sm text-[#4b5563] dark:text-muted-foreground">Has CLI</span>
          </label>
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={hasPython} onChange={(e) => setHasPython(e.target.checked)} className="rounded" />
            <span className="text-sm text-[#4b5563] dark:text-muted-foreground">Python SDK</span>
          </label>
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={hasDocker} onChange={(e) => setHasDocker(e.target.checked)} className="rounded" />
            <span className="text-sm text-[#4b5563] dark:text-muted-foreground">Docker</span>
          </label>
          <div className="pt-2 border-t border-[#e5e7eb] dark:border-border">
            <label className="text-xs font-semibold text-[#667085] dark:text-muted-foreground block mb-2">Difficulty</label>
            <select
              value={difficulty}
              onChange={(e) => setDifficulty(e.target.value)}
              className="w-full rounded-md border border-input bg-card px-2 py-1 text-sm"
            >
              <option value="">All</option>
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </div>
        </div>
      </div>
      <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-3">
        {filtered.map((tool) => (
          <SimplePanel
            key={tool.project.id}
            title={tool.project.name}
            subtitle={`${labelize(tool.profile.tool_type)} / ${tool.profile.integration_difficulty}`}
            body={tool.fit_reason ?? tool.project.description ?? ""}
            href={`/projects/tools/${encodeURIComponent(tool.project.id)}`}
          />
        ))}
      </div>
    </section>
  )
}

function CasesView({ data }: { data: ProjectsApiCaseResult }) {
  const [q, setQ] = useState("")
  const [domain, setDomain] = useState("")
  const [moduleType, setModuleType] = useState("")
  const [difficulty, setDifficulty] = useState("")

  const domains = Array.from(new Set(data.cases.map((c) => c.business_domain).filter(Boolean)))
  const moduleTypes = Array.from(new Set(data.cases.map((c) => c.module_type).filter(Boolean)))

  const filtered = data.cases.filter((c) => {
    if (q && !`${c.title} ${c.design_summary ?? ""} ${c.problem ?? ""}`.toLowerCase().includes(q.toLowerCase())) return false
    if (domain && c.business_domain !== domain) return false
    if (moduleType && c.module_type !== moduleType) return false
    if (difficulty && textValue(c.difficulty) !== difficulty) return false
    return true
  })

  if (!data.cases.length) return <ProjectEmptyState title="No Real-Derived Cases" />
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap gap-3 rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
        <div className="relative flex-1 min-w-48">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} className="pl-9 h-9" placeholder="Search cases" />
        </div>
        <select value={domain} onChange={(e) => setDomain(e.target.value)} className="rounded-md border border-input bg-card px-2 py-1 text-sm">
          <option value="">All domains</option>
          {domains.map((d) => <option key={d} value={d}>{labelize(d)}</option>)}
        </select>
        <select value={moduleType} onChange={(e) => setModuleType(e.target.value)} className="rounded-md border border-input bg-card px-2 py-1 text-sm">
          <option value="">All modules</option>
          {moduleTypes.map((m) => <option key={m} value={m}>{labelize(m)}</option>)}
        </select>
        <select value={difficulty} onChange={(e) => setDifficulty(e.target.value)} className="rounded-md border border-input bg-card px-2 py-1 text-sm">
          <option value="">All difficulty</option>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
        </select>
      </div>
      {!filtered.length ? <ProjectEmptyState title="No matching cases" /> : (
        <div className="grid gap-4 lg:grid-cols-2">
          {filtered.map((item) => (
            <Link key={item.id} href={`/projects/cases/${encodeURIComponent(item.id)}`}>
              <article className="h-full rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm transition-colors hover:border-primary/40 dark:border-border dark:bg-card">
                <div className="flex flex-wrap gap-2 mb-2">
                  <Badge variant="info">{labelize(item.module_type)}</Badge>
                  <Badge variant="muted">{labelize(item.business_domain)}</Badge>
                  {textValue(item.difficulty) ? <Badge variant="muted">{textValue(item.difficulty)}</Badge> : null}
                  {textValue(item.migration_level) ? <Badge variant="muted">reuse: {textValue(item.migration_level)}</Badge> : null}
                </div>
                <h3 className="text-base font-semibold text-[#202124] dark:text-foreground">{item.title}</h3>
                <p className="mt-2 line-clamp-3 text-sm leading-6 text-[#4b5563] dark:text-muted-foreground">
                  {String(item.design_summary ?? item.problem ?? "")}
                </p>
                {stringArray(item.suitable_for).length > 0 && (
                  <p className="mt-2 text-xs text-[#667085] dark:text-muted-foreground">
                    For: {stringArray(item.suitable_for).slice(0, 3).join(", ")}
                  </p>
                )}
              </article>
            </Link>
          ))}
        </div>
      )}
    </section>
  )
}

function CollectionsView({ data }: { data: ProjectsApiCollectionResult }) {
  const searchParams = useSearchParams()
  const [q, setQ] = useState("")
  const [tag, setTag] = useState(searchParams?.get("tag") ?? "")

  const allTags = Array.from(new Set(data.collections.flatMap((c) => stringArray(c.tags))))

  const filtered = data.collections.filter((c) => {
    if (q && !`${c.title} ${c.description}`.toLowerCase().includes(q.toLowerCase())) return false
    if (tag && !stringArray(c.tags).includes(tag)) return false
    return true
  })

  if (!data.collections.length) return <ProjectEmptyState title="No Real-Derived Collections" />
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap gap-3 rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
        <div className="relative flex-1 min-w-48">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} className="pl-9 h-9" placeholder="Search collections" />
        </div>
        {allTags.length > 0 && (
          <select value={tag} onChange={(e) => setTag(e.target.value)} className="rounded-md border border-input bg-card px-2 py-1 text-sm">
            <option value="">All tags</option>
            {allTags.map((t) => <option key={t} value={t}>{labelize(t)}</option>)}
          </select>
        )}
      </div>
      {!filtered.length ? <ProjectEmptyState title="No matching collections" /> : (
        <div className="grid gap-4 lg:grid-cols-2">
          {filtered.map((item) => (
            <Link key={item.id} href={`/projects/collections/${encodeURIComponent(item.slug)}`}>
              <article className="h-full rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm transition-colors hover:border-primary/40 dark:border-border dark:bg-card">
                <div className="flex flex-wrap gap-2 mb-2">
                  {textValue(item.collection_type) ? <Badge variant="info">{labelize(textValue(item.collection_type))}</Badge> : null}
                  <Badge variant="muted">{item.item_count ?? 0} items</Badge>
                  {stringArray(item.tags).slice(0, 3).map((t) => (
                    <Badge key={t} variant="muted">{labelize(t)}</Badge>
                  ))}
                </div>
                <h3 className="text-base font-semibold text-[#202124] dark:text-foreground">{item.title}</h3>
                <p className="mt-2 line-clamp-3 text-sm leading-6 text-[#4b5563] dark:text-muted-foreground">{item.description}</p>
                {stringArray(item.learning_goals).length > 0 && (
                  <p className="mt-2 text-xs text-[#667085] dark:text-muted-foreground">
                    Goals: {stringArray(item.learning_goals).slice(0, 2).join("; ")}
                  </p>
                )}
              </article>
            </Link>
          ))}
        </div>
      )}
    </section>
  )
}

function WatchlistView({ data, onChanged }: { data: ProjectsApiWatchlistResult; onChanged: () => void }) {
  const [projectId, setProjectId] = useState("")
  const [reason, setReason] = useState("")
  const [priority, setPriority] = useState<"low" | "medium" | "high">("medium")
  const addWatch = useMutation({
    mutationFn: () =>
      addProjectWatchlistItem({
        project_id: projectId.trim(),
        watch_reason: reason.trim(),
        priority,
      }),
    onSuccess: () => {
      void recordProjectInteraction({
        event_type: "watch_added",
        target_type: "project",
        target_id: projectId.trim(),
        action_value: priority,
      })
      setProjectId("")
      setReason("")
      setPriority("medium")
      onChanged()
    },
  })

  return (
    <section className="grid gap-5 lg:grid-cols-[minmax(0,24rem)_1fr]">
      <form
        className="space-y-3 rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card"
        onSubmit={(event) => {
          event.preventDefault()
          if (!projectId.trim() || !reason.trim()) return
          addWatch.mutate()
        }}
      >
        <h2 className="text-base font-semibold text-[#202124] dark:text-foreground">Add Watchlist Item</h2>
        <Input value={projectId} onChange={(event) => setProjectId(event.target.value)} placeholder="Real project id" />
        <Input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Watch reason" />
        <select
          value={priority}
          onChange={(event) => setPriority(event.target.value as "low" | "medium" | "high")}
          className="h-9 w-full rounded-md border border-input bg-card px-3 text-sm"
        >
          <option value="low">Low priority</option>
          <option value="medium">Medium priority</option>
          <option value="high">High priority</option>
        </select>
        <Button type="submit" disabled={!projectId.trim() || !reason.trim() || addWatch.isPending}>
          {addWatch.isPending ? "Saving" : "Watch Project"}
        </Button>
        {addWatch.isError ? <p className="text-sm text-destructive">{addWatch.error instanceof Error ? addWatch.error.message : "Watchlist update failed"}</p> : null}
      </form>
      <div className="grid gap-4 lg:grid-cols-2">
        {data.items.length ? (
          data.items.map((item) => <WatchItemCard key={item.id} item={item} />)
        ) : (
          <ProjectEmptyState title="No Watchlist Items" />
        )}
      </div>
    </section>
  )
}

function LabView({ data }: { data: ProductData }) {
  const { t } = useI18n()
  const meta = getMeta(data)
  const [problem, setProblem] = useState("")
  const [session, setSession] = useState<ProjectsLabSession | null>(null)
  const [solution, setSolution] = useState<ProjectsLabSolutionResult | null>(null)
  const [focusQuestionId, setFocusQuestionId] = useState<string | null>(null)
  const selectedCaseIds = useMemo(() => ("cases" in data ? data.cases.slice(0, 3).map((item) => item.id) : []), [data])
  const workflow = session ? presentLabWorkflow(session) : null
  const activeQuestion = session && session.next_action === "answer_question"
    ? session.questions.find((question) => session.unanswered_question_ids.includes(question.id))
    : undefined

  const startSession = useMutation({
    mutationFn: () =>
      startProjectLabSession({
        user_problem: problem.trim(),
        selected_case_ids: selectedCaseIds,
      }),
      onSuccess: (result) => {
      void recordProjectInteraction({
        event_type: "lab_started",
        target_type: "lab_session",
        target_id: result.session.id,
        query_text: problem.trim(),
      })
        setSession(result.session)
        setSolution(null)
        setFocusQuestionId(result.session.unanswered_question_ids[0] ?? null)
      },
  })
  const answerQuestion = useMutation({
    mutationFn: ({ questionId, answer }: { questionId: string; answer: string }) => {
      if (!session) throw new Error("No active Lab session")
      return answerProjectLabQuestion(session.id, { question_id: questionId, answer })
    },
    onSuccess: (result) => {
      void recordProjectInteraction({
        event_type: "question_answered",
        target_type: "lab_session",
        target_id: session?.id,
        action_value: activeQuestion?.id,
      })
      setSession(result.session)
      setFocusQuestionId(result.session.unanswered_question_ids[0] ?? null)
    },
  })
  const generateSolution = useMutation({
    mutationFn: () => {
      if (!session || !workflow?.canGenerate) throw new Error("Lab session is not ready to generate a solution")
      return generateProjectLabSolution(session.id)
    },
    onSuccess: (result) => {
      void recordProjectInteraction({
        event_type: "solution_generated",
        target_type: "lab_session",
        target_id: session?.id,
      })
      setSession(result.session)
      setSolution(result)
      setFocusQuestionId(null)
    },
    onError: (cause) => {
      if (cause instanceof ProjectsApiError && cause.status === 409) {
        const details = asRecord(cause.detail)
        const unanswered = Array.isArray(details?.unanswered_question_ids) ? details.unanswered_question_ids.find((item): item is string => typeof item === "string") : undefined
        setFocusQuestionId(unanswered ?? session?.unanswered_question_ids[0] ?? null)
      }
    },
  })

  useEffect(() => {
    if (!focusQuestionId) return
    document.getElementById(`lab-question-${focusQuestionId}`)?.focus()
  }, [focusQuestionId, session])

  return (
    <section className="space-y-6" aria-label={t("projects.lab.workspace")}>
      {meta?.data_state !== "ready" ? <ProjectDegradedNotice meta={meta ?? { source: "none", data_state: "empty", notices: [] }} /> : null}
      <form
        className="rounded-xl border border-[#dfe5df] bg-white/70 p-5 shadow-sm dark:border-border dark:bg-card sm:p-6"
        aria-busy={startSession.isPending}
        onSubmit={(event) => {
          event.preventDefault()
          if (!problem.trim()) return
          startSession.mutate()
        }}
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.14em] text-primary">{t("projects.lab.researchBrief")}</p>
            <h2 className="mt-1 text-xl font-semibold text-[#202124] dark:text-foreground">{t("projects.lab.startSession")}</h2>
          </div>
          <span className="hidden size-8 shrink-0 items-center justify-center rounded-full border border-border text-xs font-semibold text-muted-foreground sm:flex">01</span>
        </div>
        <div className="mt-5 grid gap-6 lg:grid-cols-[minmax(0,1fr)_16rem]">
          <div className="min-w-0 space-y-3">
            <p className="text-sm leading-6 text-muted-foreground">{t("projects.lab.describePrompt")}</p>
            <textarea
              id="lab-problem"
              value={problem}
              onChange={(event) => setProblem(event.target.value)}
              aria-describedby="lab-problem-help"
              aria-invalid={startSession.isError}
              className="min-h-32 w-full resize-y rounded-md border border-input bg-card px-3 py-3 text-base leading-6 shadow-none outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-primary focus-visible:ring-2 focus-visible:ring-ring"
              placeholder={t("projects.lab.problemPlaceholder")}
            />
            <p id="lab-problem-help" className="text-xs text-muted-foreground">{t("projects.lab.problemRequired")}</p>
            <div className="flex flex-wrap items-center gap-3">
              <Button type="submit" className="min-h-11" disabled={!problem.trim() || startSession.isPending}>
                {startSession.isPending ? <><Loader2 className="size-4 animate-spin" /> {t("projects.lab.starting")}</> : <>{t("projects.lab.startButton")} <ChevronRight className="size-4" /></>}
              </Button>
              {startSession.isPending ? <span className="text-sm text-muted-foreground" aria-live="polite">{t("projects.lab.creating")}</span> : null}
            </div>
          </div>
          <aside className="border-t border-border pt-4 text-sm lg:border-l lg:border-t-0 lg:pl-5 lg:pt-0">
            <p className="text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">{t("projects.lab.context")}</p>
            <p className="mt-3 text-base font-semibold text-foreground">{t("projects.lab.selectedCases", { count: selectedCaseIds.length, suffix: selectedCaseIds.length === 1 ? "" : "s" })}</p>
            <p className="mt-2 leading-6 text-muted-foreground">{t("projects.lab.apiOnly")}</p>
          </aside>
        </div>
        {startSession.isError ? <p id="lab-problem-error" role="alert" className="text-sm text-destructive">{startSession.error instanceof Error ? startSession.error.message : t("projects.lab.sessionFailed")}</p> : null}
      </form>
      {!session ? <ProjectEmptyState title={t("projects.lab.noActiveSession")} /> : (
        <div className="grid gap-6 lg:grid-cols-[minmax(13rem,0.8fr)_minmax(0,2fr)_minmax(16rem,1fr)]">
          <LabWorkflowStatus session={session} />
          <div className="min-w-0 space-y-5">
            <section className="rounded-md border border-[#d8dee7] bg-white p-5 shadow-sm dark:border-border dark:bg-card sm:p-6">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-xs font-semibold uppercase tracking-wide text-[#667085] dark:text-muted-foreground">{t("projects.lab.sessionBrief")}</p>
                  <h2 className="mt-1 text-lg font-semibold text-[#202124] dark:text-foreground">{session.user_problem}</h2>
                </div>
                <Button asChild variant="outline" size="sm"><Link href={`/projects/lab/${encodeURIComponent(session.id)}`}>{t("projects.lab.openDetail")} <ExternalLink className="size-3.5" /></Link></Button>
              </div>
              <LabClarificationList session={session} mutation={answerQuestion} hasError={answerQuestion.isError} focusQuestionId={focusQuestionId} onAnswer={(questionId, answer) => answerQuestion.mutate({ questionId, answer })} />
              {answerQuestion.isError ? <p role="alert" className="mt-3 text-sm text-destructive">{answerQuestion.error instanceof Error ? answerQuestion.error.message : t("projects.lab.answerFailed")}</p> : null}
              <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-border pt-4">
                <Button type="button" disabled={!workflow?.canGenerate || generateSolution.isPending} onClick={() => generateSolution.mutate()}>
                  {generateSolution.isPending ? <><Loader2 className="size-4 animate-spin" /> {t("projects.lab.generating")}</> : <>{t("projects.lab.generateSolution")} <ChevronRight className="size-4" /></>}
                </Button>
                <span className="text-sm text-muted-foreground" aria-live="polite">{workflow?.actionLabel}</span>
              </div>
              {generateSolution.isError ? (
                <p role="alert" className="mt-3 text-sm text-destructive">
                  {generateSolution.error instanceof Error ? generateSolution.error.message : t("projects.lab.solutionFailed")}
                  {generateSolution.error && typeof generateSolution.error === "object" && "status" in generateSolution.error && generateSolution.error.status === 409 ? ` ${t("projects.lab.returnToClarification")}` : ""}
                </p>
              ) : null}
              {solution ? <LabSolutionPanel session={solution.session} solution={solution.solution} /> : session.generated_solution || session.solution_json ? <LabSolutionPanel session={session} solution={session.solution_json ?? { markdown: session.generated_solution }} /> : null}
            </section>
          </div>
          <LabContextPanel session={session} />
        </div>
      )}
    </section>
  )
}

export function LabWorkspaceSkeleton() {
  const { t } = useI18n()
  return (
    <section className="space-y-5" aria-label={t("projects.lab.loadingWorkspace")} aria-busy="true">
      <div className="animate-pulse border-y border-[#d8dee7] bg-white py-5 dark:border-border dark:bg-card lg:rounded-md lg:border lg:px-5">
        <div className="h-3 w-24 rounded bg-secondary" />
        <div className="mt-3 h-7 w-52 rounded bg-secondary" />
        <div className="mt-5 h-28 w-full rounded-md bg-secondary" />
        <div className="mt-3 h-10 w-36 rounded-md bg-secondary" />
      </div>
      <div className="grid gap-5 lg:grid-cols-[minmax(13rem,0.8fr)_minmax(0,2fr)_minmax(16rem,1fr)]">
        <div className="animate-pulse border-y border-[#d8dee7] bg-white py-5 dark:border-border dark:bg-card lg:rounded-md lg:border lg:p-5">
          <div className="h-4 w-20 rounded bg-secondary" />
          <div className="mt-3 h-6 w-36 rounded bg-secondary" />
          <div className="mt-6 h-16 rounded-md bg-secondary" />
        </div>
        <div className="animate-pulse space-y-5 border-y border-[#d8dee7] bg-white py-5 dark:border-border dark:bg-card lg:rounded-md lg:border lg:px-5">
          <div className="h-5 w-2/3 rounded bg-secondary" />
          <div className="h-28 rounded-md bg-secondary" />
          <div className="h-28 rounded-md bg-secondary" />
        </div>
        <div className="animate-pulse border-y border-[#d8dee7] bg-white py-5 dark:border-border dark:bg-card lg:rounded-md lg:border lg:p-5">
          <div className="h-4 w-16 rounded bg-secondary" />
          <div className="mt-3 h-6 w-44 rounded bg-secondary" />
          <div className="mt-5 h-44 rounded-md bg-secondary" />
        </div>
      </div>
    </section>
  )
}

function ProjectGrid({ projects, meta }: { projects: ProjectsApiProject[]; meta: ProjectsApiMeta }) {
  if (!projects.length) return <ProjectEmptyState />
  return (
    <section className="space-y-4">
      <ProjectSourceLine meta={meta} />
      <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-3">
        {projects.map((project) => (
          <ProjectProductCard key={project.id} project={project} />
        ))}
      </div>
    </section>
  )
}

function SectionHeader({ title, href }: { title: string; href: string }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <h2 className="text-xl font-semibold text-[#202124] dark:text-foreground">{title}</h2>
      <Button asChild variant="outline" size="sm">
        <Link href={href}>
          Open
          <ArrowRight className="size-4" />
        </Link>
      </Button>
    </div>
  )
}

function SimplePanel({ title, subtitle, body, href }: { title: string; subtitle: string; body: string; href?: string }) {
  const content = (
    <article className="h-full rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm transition-colors hover:border-primary/40 dark:border-border dark:bg-card">
      <p className="text-xs font-semibold uppercase text-[#667085] dark:text-muted-foreground">{subtitle}</p>
      <h3 className="mt-2 text-base font-semibold text-[#202124] dark:text-foreground">{title}</h3>
      <p className="mt-2 line-clamp-3 text-sm leading-6 text-[#4b5563] dark:text-muted-foreground">{body || "No additional text was available from the real Project Radar artifact."}</p>
    </article>
  )
  return href ? <Link href={href}>{content}</Link> : content
}

function HeroPill({ label, value }: { label: string; value: string | number }) {
  return (
    <span className="inline-flex items-center gap-2 rounded-full border border-[#dbe5dd] bg-white/90 px-3 py-1 text-xs text-[#334155]/70 shadow-sm dark:border-border dark:bg-card dark:text-muted-foreground">
      <span className="font-medium text-[#334155] dark:text-foreground">{label}</span>
      {value}
    </span>
  )
}

type ProductData =
  | ProjectsApiHomeResult
  | ProjectsApiListResult
  | ProjectsApiToolResult
  | ProjectsApiCaseResult
  | ProjectsApiCollectionResult
  | ProjectsApiWatchlistResult

function getMeta(data: ProductData): ProjectsApiMeta | undefined {
  return "meta" in data ? data.meta : undefined
}

function firstProject(data: ProductData): ProjectsApiProject | undefined {
  if ("hot" in data) return data.hot[0] ?? data.rising[0] ?? data.tools[0]
  if ("page" in data && "items" in data) return data.items[0]
  if ("tools" in data) return data.tools[0]?.project
  return undefined
}

function projectCount(data: ProductData): number {
  if ("hot" in data) return uniqueProjects([...data.hot, ...data.rising, ...data.tools]).length
  if ("page" in data && "items" in data) return data.page.total
  if ("tools" in data) return data.tools.length
  return 0
}

function caseCount(data: ProductData): number {
  if ("cases" in data) return data.cases.length
  return 0
}

function collectionCount(data: ProductData): number {
  if ("collections" in data) return data.collections.length
  return 0
}

function uniqueProjects(projects: ProjectsApiProject[]): ProjectsApiProject[] {
  const seen = new Set<string>()
  const result: ProjectsApiProject[] = []
  for (const project of projects) {
    if (seen.has(project.id)) continue
    seen.add(project.id)
    result.push(project)
  }
  return result
}

function WatchItemCard({ item }: { item: ProjectsApiWatchlistItem }) {
  const signals = recordArray(item.signals)
  const watchTopics = stringArray(item.watch_topics)
  const lastChangeSummary = textValue(item.last_change_summary)
  const pc = item.priority === "high" ? "text-red-600" : item.priority === "medium" ? "text-amber-600" : "text-[#667085]"
  return (
    <article className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate font-mono text-xs text-[#667085]">{item.project_id}</p>
          <p className="mt-1 text-sm font-semibold text-[#202124] dark:text-foreground">{item.watch_reason}</p>
        </div>
        <span className={`shrink-0 text-xs font-semibold ${pc}`}>{item.priority}</span>
      </div>
      <div className="mt-2 flex flex-wrap gap-1">
        <Badge variant={item.status === "active" ? "info" : "muted"}>{item.status}</Badge>
        {watchTopics.map((t) => <Badge key={t} variant="muted">{t}</Badge>)}
      </div>
      {lastChangeSummary ? (
        <p className="mt-2 text-xs text-[#4b5563] dark:text-muted-foreground">{lastChangeSummary}</p>
      ) : null}
      {signals.length > 0 && (
        <div className="mt-3 border-t border-[#e5e7eb] pt-3 dark:border-border">
          <p className="mb-2 text-xs font-semibold text-[#667085]">Signals</p>
          <div className="space-y-2">
            {signals.map((signal, i) => (
              <div key={i} className="flex items-start gap-2">
                <span className={`mt-1 size-2 shrink-0 rounded-full ${signal.severity === "high" ? "bg-red-500" : signal.severity === "medium" ? "bg-amber-400" : "bg-[#d1d5db]"}`} />
                <div className="min-w-0">
                  <p className="text-xs font-medium text-[#202124] dark:text-foreground">{textValue(signal.title)}</p>
                  <p className="text-xs text-[#667085]">{textValue(signal.summary)}</p>
                  {textValue(signal.source_url) ? <a href={textValue(signal.source_url)} target="_blank" rel="noreferrer" className="text-xs text-primary hover:underline">source ↗</a> : null}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </article>
  )
}

const NODE_COLORS: Record<string, string> = {
  user_problem: "#0f172a", case: "#2563eb", solution: "#16a34a",
  question: "#d97706", feedback: "#7c3aed", pattern: "#0891b2", component: "#be185d",
}

export function LabWorkflowStatus({ session }: { session: ProjectsLabSession }) {
  const { t } = useI18n()
  const workflow = presentLabWorkflow(session)
  const stageLabel = workflow.isUnknown ? `${t("projects.lab.unsupportedStageLabel")}: ${session.raw_current_stage ?? "unknown"}` : t(`projects.lab.stage.${session.current_stage}`)
  const actionLabel = workflow.isUnknown ? t("projects.lab.actionsDisabled") : t(`projects.lab.action.${session.next_action}`)
  return (
    <aside className="h-fit border-y border-[#d8dee7] bg-white py-5 dark:border-border dark:bg-card lg:rounded-md lg:border lg:p-5" aria-label={t("projects.lab.workflow")}>
      <div className="flex items-start gap-3">
        <span className={`mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-full ${workflow.statusTone === "success" ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-500/20 dark:text-emerald-300" : workflow.statusTone === "warning" ? "bg-amber-100 text-amber-700 dark:bg-amber-500/20 dark:text-amber-300" : "bg-secondary text-muted-foreground"}`}>
          {workflow.statusTone === "success" ? <Check className="size-4" /> : workflow.statusTone === "warning" ? <AlertCircle className="size-4" /> : <CircleHelp className="size-4" />}
        </span>
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-wide text-[#667085] dark:text-muted-foreground">{t("projects.lab.workflow")}</p>
          <h2 className="mt-1 text-base font-semibold text-[#202124] dark:text-foreground">{stageLabel}</h2>
          <p className="mt-2 text-sm leading-6 text-muted-foreground" aria-live="polite">{actionLabel}</p>
        </div>
      </div>
      {workflow.unansweredCount > 0 ? <p className="mt-4 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-500/10 dark:text-amber-100">{t("projects.lab.remaining", { count: workflow.unansweredCount, suffix: workflow.unansweredCount === 1 ? "" : "s" })}</p> : null}
      {workflow.isUnknown ? <p className="mt-4 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-500/10 dark:text-amber-100">{t("projects.lab.actionsDisabled")}</p> : null}
      <span className="sr-only">{session.current_stage}</span>
    </aside>
  )
}

export function LabClarificationList({
  session,
  focusQuestionId,
  onAnswer,
  mutation,
  hasError,
}: {
  session: ProjectsLabSession
  focusQuestionId?: string | null
  onAnswer: (questionId: string, answer: string) => void
  mutation: { isPending: boolean }
  hasError?: boolean
}) {
  const { t } = useI18n()
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const unanswered = new Set(session.unanswered_question_ids)
  const firstUnansweredId = session.questions.find((question) => unanswered.has(question.id))?.id
  if (!session.questions.length) return <p className="mt-5 text-sm text-muted-foreground">{t("projects.lab.noQuestions")}</p>
  return (
    <div className="mt-5 space-y-3" aria-label={t("projects.lab.questions")}>
      {session.questions.map((question, index) => {
        const answered = !unanswered.has(question.id) || labQuestionAnswered(question.answered_value)
        const draft = drafts[question.id] ?? ""
        const active = !answered && question.id === firstUnansweredId
        return (
          <div key={question.id} className={`rounded-md border p-4 ${answered ? "border-border bg-secondary/30" : "border-primary/40 bg-card"}`}>
            <div className="flex items-start gap-3">
              <span className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full bg-secondary text-xs font-semibold text-muted-foreground">{index + 1}</span>
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-[#202124] dark:text-foreground">{question.question}</p>
                {answered ? <p className="mt-2 flex items-center gap-1 text-sm text-muted-foreground"><Check className="size-3.5 text-emerald-600" /> {String(question.answered_value ?? t("projects.lab.answer"))}</p> : !active ? <p className="mt-2 text-xs text-muted-foreground">{t("projects.lab.waitingPrevious")}</p> : (
                  <div className="mt-3 space-y-2">
                    {question.options?.length ? <div className="flex flex-wrap gap-2">{question.options.map((option) => <button key={option.value} type="button" className="min-h-11 rounded-md border border-border px-3 text-sm hover:bg-secondary" onClick={() => setDrafts((current) => ({ ...current, [question.id]: option.value }))}>{option.label}</button>)}</div> : null}
                    <label className="sr-only" htmlFor={`lab-question-${question.id}`}>Answer: {question.question}</label>
                    <div className="flex flex-col gap-2 sm:flex-row">
                      <Input
                        id={`lab-question-${question.id}`}
                        autoFocus={focusQuestionId === question.id}
                        value={draft}
                        onChange={(event) => setDrafts((current) => ({ ...current, [question.id]: event.target.value }))}
                        placeholder={t("projects.lab.answerPlaceholder")}
                        aria-required={question.required !== false}
                        aria-invalid={Boolean(hasError)}
                        aria-describedby={hasError ? `lab-question-${question.id}-error` : undefined}
                        disabled={mutation.isPending}
                      />
                      <Button type="button" variant="outline" className="min-h-11" disabled={!draft.trim() || mutation.isPending} onClick={() => onAnswer(question.id, draft.trim())}>
                        {mutation.isPending ? <><Loader2 className="size-4 animate-spin" /> {t("projects.lab.savingAnswer")}</> : t("projects.lab.answer")}
                      </Button>
                    </div>
                    {hasError ? <p id={`lab-question-${question.id}-error`} className="text-sm text-destructive" role="alert">{t("projects.lab.answerError")}</p> : null}
                  </div>
                )}
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}

export function LabContextPanel({ session }: { session: ProjectsLabSession }) {
  const { t } = useI18n()
  return (
    <aside className="min-w-0 space-y-5 border-y border-[#d8dee7] bg-white py-5 dark:border-border dark:bg-card lg:rounded-md lg:border lg:p-5">
      <div>
        <p className="text-xs font-semibold uppercase tracking-wide text-[#667085] dark:text-muted-foreground">{t("projects.lab.context")}</p>
        <h2 className="mt-1 text-lg font-semibold text-[#202124] dark:text-foreground">{t("projects.lab.references")}</h2>
      </div>
      <div className="rounded-md bg-secondary/40 p-3 text-sm">
        <p className="font-medium text-[#202124] dark:text-foreground">{t("projects.lab.selectedCases", { count: session.selected_case_ids.length, suffix: session.selected_case_ids.length === 1 ? "" : "s" })}</p>
        <p className="mt-1 text-muted-foreground">{t("projects.lab.apiOnly")}</p>
      </div>
      <LabGraph sessionId={session.id} graph={session.graph_state} />
    </aside>
  )
}

export function LabGraph({ sessionId, graph }: { sessionId?: string; graph?: unknown }) {
  const { t } = useI18n()
  const record = asRecord(graph)
  const nodes = recordArray(record?.nodes)
  const edges = recordArray(record?.edges)
  const focused = stringArray(record?.focused_node_ids)
  const [selectedNode, setSelectedNode] = useState<string | null>(null)
  const [explanation, setExplanation] = useState<{ title: string; explanation: string; related_nodes: Array<Record<string, unknown>> } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const explain = useMutation({
    mutationFn: (nodeId: string) => {
      if (!sessionId) throw new Error("Lab session is unavailable")
      return explainProjectLabNode(sessionId, { node_id: nodeId, style: "plain" })
    },
    onSuccess: (result) => { setExplanation(result); setError(null) },
    onError: (cause) => setError(cause instanceof Error ? cause.message : t("projects.lab.explanationFailed")),
  })
  if (!record) return <p className="text-sm text-muted-foreground">{t("projects.lab.graphUnavailable")}</p>
  if (!nodes.length) return <p className="text-sm text-muted-foreground">{t("projects.lab.noGraphNodes")}</p>
  const cx = 200, cy = 120, radius = 90
  const positions: Record<string, { x: number; y: number }> = {}
  nodes.forEach((node, index) => {
    const angle = (2 * Math.PI * index) / nodes.length - Math.PI / 2
    const id = textValue(node.id)
    if (id) positions[id] = { x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) }
  })
  return (
    <section aria-labelledby="lab-graph-title">
      <div className="flex items-baseline justify-between gap-2">
        <h3 id="lab-graph-title" className="text-sm font-semibold text-[#202124] dark:text-foreground">{t("projects.lab.graphContext")}</h3>
        <span className="text-xs text-muted-foreground">{t("projects.lab.graphSummary", { nodes: nodes.length, edges: edges.length })}</span>
      </div>
      <div className="mt-3 overflow-hidden rounded-md border border-border bg-[#f8fafc] p-2 dark:bg-background">
        <svg viewBox="0 0 400 240" className="w-full" role="img" aria-label={t("projects.lab.graphAria", { nodes: nodes.length, edges: edges.length })} style={{ maxHeight: 200 }}>
          {edges.map((edge, index) => {
            const source = positions[textValue(edge.source_id)]
            const target = positions[textValue(edge.target_id)]
            return source && target ? <line key={index} x1={source.x} y1={source.y} x2={target.x} y2={target.y} stroke="#cbd5e1" strokeWidth={1.5} /> : null
          })}
          {nodes.map((node) => {
            const id = textValue(node.id)
            const position = positions[id]
            if (!position) return null
            const active = focused.includes(id) || selectedNode === id
            const color = NODE_COLORS[textValue(node.node_type)] ?? "#6b7280"
            return <g key={id}><circle cx={position.x} cy={position.y} r={active ? 10 : 7} fill={color} opacity={active ? 1 : 0.7} /><text x={position.x} y={position.y + 18} textAnchor="middle" fontSize={8} fill="currentColor">{textValue(node.title).slice(0, 18)}</text></g>
          })}
        </svg>
      </div>
      <ol className="mt-3 space-y-2" aria-label={t("projects.lab.graphNodesRelationships")}>
        {nodes.map((node) => {
          const id = textValue(node.id)
          const related = edges.filter((edge) => textValue(edge.source_id) === id || textValue(edge.target_id) === id).length
          return <li key={id} className="flex items-center gap-2 text-sm"><button type="button" className="min-h-11 min-w-0 flex-1 rounded-md border border-transparent px-2 text-left hover:border-border hover:bg-secondary" aria-pressed={selectedNode === id} onClick={() => setSelectedNode(id)}><span className="font-medium text-[#202124] dark:text-foreground">{textValue(node.title) || t("projects.lab.unnamedNode")}</span><span className="ml-2 text-xs text-muted-foreground">{textValue(node.node_type)} · {t("projects.lab.related", { count: related })}</span></button><Button type="button" variant="ghost" size="icon" aria-label={t("projects.lab.explain", { title: textValue(node.title) })} disabled={explain.isPending} onClick={() => explain.mutate(id)}><Info className="size-4" /></Button></li>
        })}
      </ol>
      {explain.isPending ? <p className="mt-3 text-sm text-muted-foreground" aria-live="polite">{t("projects.lab.loadingExplanation")}</p> : null}
      {error ? <p className="mt-3 text-sm text-destructive" role="alert">{error}</p> : null}
      {explanation ? <div className="mt-3 rounded-md border border-primary/30 bg-primary/5 p-3" aria-live="polite"><p className="text-sm font-semibold text-[#202124] dark:text-foreground">{explanation.title}</p><p className="mt-1 text-sm leading-6 text-muted-foreground">{explanation.explanation}</p>{explanation.related_nodes.length ? <p className="mt-2 text-xs text-muted-foreground">{explanation.related_nodes.length} related node{explanation.related_nodes.length === 1 ? "" : "s"}</p> : null}</div> : null}
    </section>
  )
}

export function LabSolutionPanel({ session, solution }: { session: ProjectsLabSession; solution: Record<string, unknown> }) {
  const { t } = useI18n()
  const [copyState, setCopyState] = useState<"idle" | "success" | "error">("idle")
  const structured = labSolutionValue(session) ?? solution
  const copyStructured = async () => {
    const text = JSON.stringify(structured, null, 2)
    try {
      if (!navigator.clipboard) throw new Error("Clipboard is unavailable")
      await navigator.clipboard.writeText(text)
      setCopyState("success")
    } catch {
      setCopyState("error")
    }
  }
  const evidence = asRecord(structured)
  const summaryText = session.generated_solution || textValue(evidence?.summary) || t("projects.lab.unavailable")
  const structuredKeys = Object.keys(structured)
  const evidenceKeys = ["case_ids", "project_ids", "data_policy", "review_notes"]
  return (
    <section className="mt-5 border-t border-border pt-5" aria-labelledby="lab-solution-title">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-xs font-semibold uppercase tracking-wide text-[#667085] dark:text-muted-foreground">{t("projects.lab.output")}</p><h2 id="lab-solution-title" className="mt-1 text-lg font-semibold text-[#202124] dark:text-foreground">{t("projects.lab.generatedSolution")}</h2></div><Button asChild variant="outline" size="sm"><Link href={`/projects/lab/${encodeURIComponent(session.id)}`}>{t("projects.lab.openSessionDetail")} <ExternalLink className="size-3.5" /></Link></Button></div>
      <Tabs defaultValue="summary" className="mt-4"><TabsList className="max-w-full overflow-x-auto"><TabsTrigger value="summary">{t("projects.lab.summary")}</TabsTrigger><TabsTrigger value="structured">{t("projects.lab.structured")}</TabsTrigger><TabsTrigger value="evidence">{t("projects.lab.evidence")}</TabsTrigger></TabsList>
        <TabsContent value="summary"><div className="rounded-md border border-border bg-secondary/30 p-4"><p className="whitespace-pre-wrap text-sm leading-7 text-[#334155] dark:text-muted-foreground">{summaryText}</p>{textValue(evidence?.module) ? <p className="mt-3 text-sm font-medium text-[#334155] dark:text-foreground">Module: {textValue(evidence?.module)}</p> : null}<p className="mt-3 text-xs text-muted-foreground">Structured fields: {structuredKeys.length ? structuredKeys.join(", ") : "Unavailable"}</p></div></TabsContent>
        <TabsContent value="structured"><div className="relative"><pre className="max-h-80 overflow-auto rounded-md bg-[#111827] p-4 pr-14 text-xs leading-5 text-white">{JSON.stringify(structured, null, 2)}</pre><Button type="button" variant="secondary" size="icon" className="absolute right-2 top-2" aria-label={t("projects.lab.copyStructured")} title={t("projects.lab.copyStructured")} onClick={copyStructured}><Clipboard className="size-4" /></Button></div><p className="mt-2 text-xs text-muted-foreground" aria-live="polite">{copyState === "success" ? t("projects.lab.copySuccess") : copyState === "error" ? t("projects.lab.copyError") : t("projects.lab.copyHint")}</p></TabsContent>
        <TabsContent value="evidence"><dl className="grid gap-2 sm:grid-cols-2">{evidenceKeys.map((key) => <div key={key} className="rounded-md border border-border bg-secondary/30 p-3"><dt className="text-xs font-semibold uppercase text-muted-foreground">{key.replaceAll("_", " ")}</dt><dd className="mt-1 break-words text-sm text-[#334155] dark:text-foreground">{evidence?.[key] === undefined ? "Unavailable from the API response." : Array.isArray(evidence[key]) ? evidence[key].join(", ") : String(evidence[key])}</dd></div>)}</dl></TabsContent>
      </Tabs>
    </section>
  )
}

function asRecord(value: unknown): UnknownRecord | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as UnknownRecord : null
}

function textValue(value: unknown): string {
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return ""
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(textValue).filter(Boolean) : []
}

function recordArray(value: unknown): UnknownRecord[] {
  return Array.isArray(value) ? value.map(asRecord).filter((item): item is UnknownRecord => Boolean(item)) : []
}
