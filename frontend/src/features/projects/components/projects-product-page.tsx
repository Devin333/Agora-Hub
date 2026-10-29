"use client"

import { ResearchMaterialContext } from "@/components/research/research-material-context"

import Link from "next/link"
import { AlertCircle, ArrowRight, Binoculars, Boxes, Check, ChevronRight, CircleHelp, Clipboard, ExternalLink, FlaskConical, Flame, FolderKanban, GitBranch, Info, Loader2, Sparkles, Star } from "lucide-react"
import type { ComponentType } from "react"
import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { usePathname, useRouter, useSearchParams } from "next/navigation"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
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
import {
  ProjectCodeBlock,
  ProjectEmptyText,
  ProjectFilterChip,
  ProjectNativeSelect,
  ProjectNativeSelectOption,
  ProjectPage,
  ProjectPageBody,
  ProjectPageHeader,
  ProjectPanel,
  ProjectSearchInput,
  ProjectSectionHeading,
  ProjectStat,
  projectCardClassName,
  projectCardLinkClassName,
  projectGridClassName,
  projectInsetClassName,
} from "@/features/projects/components/project-page-layout"
import { labGraphEdgeColor, labNodeColor, levelTone, toneDotClass, toneSurfaceClass, toneTextClass, workflowTone } from "@/features/projects/components/project-tone"
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
    <ProjectPage>
      <ProjectHero section={section} route={route} data={data} query={query} onQueryChange={setQuery} onSearch={submitQuery} isFetching={isFetching} sourceQuestion={sourceQuestion} />
      <ProjectPageBody>
        {meta ? <ProjectDegradedNotice meta={meta} /> : null}
        <RouteContent route={route} data={data} onRefresh={() => void refetch()} />
      </ProjectPageBody>
    </ProjectPage>
  )
}

function LabRoute({ data, section, isFetching }: { data: ProductData; section: ProjectProductSection; isFetching: boolean }) {
  const { t } = useI18n()
  const meta = getMeta(data)

  return (
    <ProjectPage>
      <ProjectPageHeader
        eyebrow={t("projects.lab.headerEyebrow")}
        title={section.title}
        icon={<FlaskConical className="size-4" strokeWidth={1.8} />}
        subtitle={t("projects.lab.headerDescription")}
        actions={isFetching ? <span className="inline-flex items-center gap-1.5 text-label text-muted-foreground" role="status"><Loader2 className="size-3.5 animate-spin" aria-hidden="true" /> {t("projects.lab.refreshing")}</span> : null}
      >
        <div className="flex flex-wrap items-center gap-1.5">
          <ProjectStat label={t("projects.lab.statsCases")} value={caseCount(data)} />
          <ProjectStat label={t("projects.lab.statsProjects")} value={projectCount(data)} />
          <ProjectStat label={t("projects.lab.statsState")} value={meta?.data_state ?? "empty"} />
        </div>
        {meta ? <ProjectSourceLine meta={meta} /> : null}
      </ProjectPageHeader>
      <ProjectPageBody>
        <LabView data={data} />
      </ProjectPageBody>
    </ProjectPage>
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
    <ProjectPageHeader
      title={section.title}
      icon={<Icon className="size-4" />}
      subtitle={section.description}
      leading={
        <>
          <Badge variant="info">Projects API v1</Badge>
          <Badge variant="muted">Real Project Radar data</Badge>
          {isFetching ? <Badge variant="muted">Refreshing</Badge> : null}
        </>
      }
    >
      {sourceQuestion ? (
        <p className="rounded-lg bg-muted px-3 py-2 text-body2 text-muted-foreground">
          来自首页的问题：<span className="font-medium text-foreground">{sourceQuestion}</span>
        </p>
      ) : null}
      <ResearchMaterialContext />
      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_20rem] lg:items-start">
        <div className="min-w-0 space-y-3">
          <div className="flex flex-wrap gap-1.5">
            <ProjectStat label="Projects" value={projectCount(data)} />
            <ProjectStat label="Cases" value={caseCount(data)} />
            <ProjectStat label="Collections" value={collectionCount(data)} />
            <ProjectStat label="State" value={meta?.data_state ?? "empty"} />
          </div>
          <form className="flex max-w-2xl items-center gap-2" onSubmit={event => { event.preventDefault(); onSearch() }}>
            <label className="sr-only" htmlFor="projects-product-search">
              Search projects
            </label>
            <ProjectSearchInput
              className="flex-1"
              id="projects-product-search"
              value={query}
              onChange={(event) => onQueryChange(event.target.value)}
              onKeyDown={event => { if (event.key === "Enter" && (event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault() }}
              placeholder="Search real projects, tools, cases, or module themes"
            />
            <Button type="submit" size="lg" className="shrink-0">搜索</Button>
          </form>
        </div>

        <aside className="min-w-0 rounded-xl border border-border bg-card p-3">
          <p className="text-label text-muted-foreground">Current focus</p>
          {lead ? (
            <div className="mt-1 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 className="line-clamp-2 text-body1 font-semibold text-foreground">{lead.name}</h2>
                <p className="mt-0.5 text-label text-muted-foreground">
                  Score {formatScore(lead.hot_score ?? lead.rising_score ?? undefined)} / Sources {lead.source_count}
                </p>
              </div>
              <Button asChild size="sm" variant="outline" className="shrink-0">
                <Link href={`/projects/${encodeURIComponent(lead.slug)}`}>
                  View project
                  <ArrowRight strokeWidth={1.8} />
                </Link>
              </Button>
            </div>
          ) : (
            <p className="mt-1 text-body2 text-muted-foreground">No real Project Radar output is available yet.</p>
          )}
        </aside>
      </div>
    </ProjectPageHeader>
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
    <div className="space-y-6">
      <section className="space-y-3">
        <SectionHeader title="Hot and rising" href="/projects/hot" />
        <ProjectGrid projects={projects.slice(0, 6)} meta={data.meta} />
      </section>
      <section className="space-y-3">
        <SectionHeader title="Cases and collections" href="/projects/cases" />
        <div className={projectGridClassName}>
          {data.cases.slice(0, 4).map((item) => (
            <SimplePanel key={item.id} title={String(item.title)} subtitle={String(item.module_type)} body={String(item.design_summary ?? item.problem ?? "")} />
          ))}
          {data.collections.slice(0, 4).map((item) => (
            <SimplePanel key={item.id} title={item.title} subtitle={`${item.item_count ?? 0} items`} body={item.description} href={`/projects/collections`} />
          ))}
        </div>
      </section>
    </div>
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
    <section className="space-y-4">
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filters">
        <ProjectFilterChip active={hasApi} onClick={() => setHasApi((value) => !value)}>Has API</ProjectFilterChip>
        <ProjectFilterChip active={hasCli} onClick={() => setHasCli((value) => !value)}>Has CLI</ProjectFilterChip>
        <ProjectFilterChip active={hasPython} onClick={() => setHasPython((value) => !value)}>Python SDK</ProjectFilterChip>
        <ProjectFilterChip active={hasDocker} onClick={() => setHasDocker((value) => !value)}>Docker</ProjectFilterChip>
        <ProjectNativeSelect aria-label="Difficulty" value={difficulty} onChange={(e) => setDifficulty(e.target.value)} className="text-[13px]">
          <ProjectNativeSelectOption value="">All difficulty</ProjectNativeSelectOption>
          <ProjectNativeSelectOption value="low">Low</ProjectNativeSelectOption>
          <ProjectNativeSelectOption value="medium">Medium</ProjectNativeSelectOption>
          <ProjectNativeSelectOption value="high">High</ProjectNativeSelectOption>
        </ProjectNativeSelect>
      </div>
      {!filtered.length ? <ProjectEmptyText title="No matching tools" /> : (
        <div className={projectGridClassName}>
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
      )}
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
      <div className="flex flex-wrap items-center gap-2">
        <ProjectSearchInput className="w-full max-w-[260px]" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search cases" aria-label="Search cases" />
        <ProjectNativeSelect aria-label="Domain" value={domain} onChange={(e) => setDomain(e.target.value)} className="text-[13px]">
          <ProjectNativeSelectOption value="">All domains</ProjectNativeSelectOption>
          {domains.map((d) => <ProjectNativeSelectOption key={d} value={d}>{labelize(d)}</ProjectNativeSelectOption>)}
        </ProjectNativeSelect>
        <ProjectNativeSelect aria-label="Module" value={moduleType} onChange={(e) => setModuleType(e.target.value)} className="text-[13px]">
          <ProjectNativeSelectOption value="">All modules</ProjectNativeSelectOption>
          {moduleTypes.map((m) => <ProjectNativeSelectOption key={m} value={m}>{labelize(m)}</ProjectNativeSelectOption>)}
        </ProjectNativeSelect>
        <ProjectNativeSelect aria-label="Difficulty" value={difficulty} onChange={(e) => setDifficulty(e.target.value)} className="text-[13px]">
          <ProjectNativeSelectOption value="">All difficulty</ProjectNativeSelectOption>
          <ProjectNativeSelectOption value="low">Low</ProjectNativeSelectOption>
          <ProjectNativeSelectOption value="medium">Medium</ProjectNativeSelectOption>
          <ProjectNativeSelectOption value="high">High</ProjectNativeSelectOption>
        </ProjectNativeSelect>
      </div>
      {!filtered.length ? <ProjectEmptyText title="No matching cases" /> : (
        <div className={projectGridClassName}>
          {filtered.map((item) => (
            <Link key={item.id} href={`/projects/cases/${encodeURIComponent(item.id)}`} className={projectCardLinkClassName}>
              <article className={projectCardClassName}>
                <div className="mb-2 flex flex-wrap gap-1.5">
                  <Badge variant="info">{labelize(item.module_type)}</Badge>
                  <Badge variant="muted">{labelize(item.business_domain)}</Badge>
                  {textValue(item.difficulty) ? <Badge variant="muted">{textValue(item.difficulty)}</Badge> : null}
                  {textValue(item.migration_level) ? <Badge variant="muted">reuse: {textValue(item.migration_level)}</Badge> : null}
                </div>
                <h3 className="break-words text-[15px] font-semibold text-foreground">{item.title}</h3>
                <p className="mt-1 line-clamp-3 text-body2 text-muted-foreground">{String(item.design_summary ?? item.problem ?? "")}</p>
                {stringArray(item.suitable_for).length > 0 && (
                  <p className="mt-auto pt-3 text-label text-faint-foreground">For: {stringArray(item.suitable_for).slice(0, 3).join(", ")}</p>
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
      <div className="flex flex-wrap items-center gap-2">
        <ProjectSearchInput className="w-full max-w-[260px]" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search collections" aria-label="Search collections" />
        {allTags.length > 0 && (
          <ProjectNativeSelect aria-label="Tag" value={tag} onChange={(e) => setTag(e.target.value)} className="text-[13px]">
            <ProjectNativeSelectOption value="">All tags</ProjectNativeSelectOption>
            {allTags.map((t) => <ProjectNativeSelectOption key={t} value={t}>{labelize(t)}</ProjectNativeSelectOption>)}
          </ProjectNativeSelect>
        )}
      </div>
      {!filtered.length ? <ProjectEmptyText title="No matching collections" /> : (
        <div className={projectGridClassName}>
          {filtered.map((item) => (
            <Link key={item.id} href={`/projects/collections/${encodeURIComponent(item.slug)}`} className={projectCardLinkClassName}>
              <article className={projectCardClassName}>
                <div className="mb-2 flex flex-wrap gap-1.5">
                  {textValue(item.collection_type) ? <Badge variant="info">{labelize(textValue(item.collection_type))}</Badge> : null}
                  <Badge variant="muted">{item.item_count ?? 0} items</Badge>
                  {stringArray(item.tags).slice(0, 3).map((t) => (
                    <Badge key={t} variant="muted">{labelize(t)}</Badge>
                  ))}
                </div>
                <h3 className="break-words text-[15px] font-semibold text-foreground">{item.title}</h3>
                <p className="mt-1 line-clamp-3 text-body2 text-muted-foreground">{item.description}</p>
                {stringArray(item.learning_goals).length > 0 && (
                  <p className="mt-auto pt-3 text-label text-faint-foreground">Goals: {stringArray(item.learning_goals).slice(0, 2).join("; ")}</p>
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
    <section className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)] lg:items-start">
      <ProjectPanel title="Add Watchlist Item">
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault()
            if (!projectId.trim() || !reason.trim()) return
            addWatch.mutate()
          }}
        >
          <Input value={projectId} onChange={(event) => setProjectId(event.target.value)} placeholder="Real project id" aria-label="Project id" />
          <Input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Watch reason" aria-label="Watch reason" />
          <ProjectNativeSelect
            aria-label="Priority"
            value={priority}
            onChange={(event) => setPriority(event.target.value as "low" | "medium" | "high")}
            wrapperClassName="w-full"
          >
            <ProjectNativeSelectOption value="low">Low priority</ProjectNativeSelectOption>
            <ProjectNativeSelectOption value="medium">Medium priority</ProjectNativeSelectOption>
            <ProjectNativeSelectOption value="high">High priority</ProjectNativeSelectOption>
          </ProjectNativeSelect>
          <Button type="submit" disabled={!projectId.trim() || !reason.trim() || addWatch.isPending}>
            {addWatch.isPending ? "Saving" : "Watch Project"}
          </Button>
          {addWatch.isError ? <p role="alert" className="text-body2 text-destructive-text">{addWatch.error instanceof Error ? addWatch.error.message : "Watchlist update failed"}</p> : null}
        </form>
      </ProjectPanel>
      {data.items.length ? (
        <div className="grid gap-3 sm:grid-cols-2">
          {data.items.map((item) => <WatchItemCard key={item.id} item={item} />)}
        </div>
      ) : (
        <ProjectEmptyState title="No Watchlist Items" />
      )}
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
    <section className="space-y-4" aria-label={t("projects.lab.workspace")}>
      {meta?.data_state !== "ready" ? <ProjectDegradedNotice meta={meta ?? { source: "none", data_state: "empty", notices: [] }} /> : null}
      <form
        className="rounded-xl border border-border bg-card p-4 md:p-5"
        aria-busy={startSession.isPending}
        onSubmit={(event) => {
          event.preventDefault()
          if (!problem.trim()) return
          startSession.mutate()
        }}
      >
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="text-label text-muted-foreground">{t("projects.lab.researchBrief")}</p>
            <h2 className="mt-0.5 text-heading3 font-semibold text-foreground">{t("projects.lab.startSession")}</h2>
          </div>
          <span className="hidden size-8 shrink-0 place-items-center rounded-full bg-muted text-caption font-semibold text-muted-foreground sm:grid" aria-hidden="true">01</span>
        </div>
        <div className="mt-4 grid gap-5 lg:grid-cols-[minmax(0,1fr)_16rem]">
          <div className="min-w-0 space-y-3">
            <p className="text-body2 text-muted-foreground">{t("projects.lab.describePrompt")}</p>
            <textarea
              id="lab-problem"
              value={problem}
              onChange={(event) => setProblem(event.target.value)}
              aria-describedby="lab-problem-help"
              aria-invalid={startSession.isError}
              className="min-h-32 w-full resize-y rounded-lg border border-input bg-transparent px-3 py-2.5 text-body1 text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 aria-invalid:border-destructive dark:bg-input/30"
              placeholder={t("projects.lab.problemPlaceholder")}
            />
            <p id="lab-problem-help" className="text-label text-muted-foreground">{t("projects.lab.problemRequired")}</p>
            <div className="flex flex-wrap items-center gap-3">
              <Button type="submit" size="lg" disabled={!problem.trim() || startSession.isPending}>
                {startSession.isPending ? <><Loader2 className="animate-spin" strokeWidth={1.8} /> {t("projects.lab.starting")}</> : <>{t("projects.lab.startButton")} <ChevronRight strokeWidth={1.8} /></>}
              </Button>
              {startSession.isPending ? <span className="text-body2 text-muted-foreground" aria-live="polite">{t("projects.lab.creating")}</span> : null}
            </div>
          </div>
          <aside className="border-t border-border pt-4 text-body2 lg:border-s lg:border-t-0 lg:ps-5 lg:pt-0">
            <p className="text-label text-muted-foreground">{t("projects.lab.context")}</p>
            <p className="mt-1 text-body1 font-semibold text-foreground">{t("projects.lab.selectedCases", { count: selectedCaseIds.length, suffix: selectedCaseIds.length === 1 ? "" : "s" })}</p>
            <p className="mt-1.5 text-muted-foreground">{t("projects.lab.apiOnly")}</p>
          </aside>
        </div>
        {startSession.isError ? <p id="lab-problem-error" role="alert" className="mt-3 text-body2 text-destructive-text">{startSession.error instanceof Error ? startSession.error.message : t("projects.lab.sessionFailed")}</p> : null}
      </form>
      {!session ? <ProjectEmptyState title={t("projects.lab.noActiveSession")} /> : (
        <div className="grid gap-4 lg:grid-cols-[minmax(13rem,0.8fr)_minmax(0,2fr)_minmax(16rem,1fr)]">
          <LabWorkflowStatus session={session} />
          <section className="min-w-0 rounded-xl border border-border bg-card p-4 md:p-5">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-label text-muted-foreground">{t("projects.lab.sessionBrief")}</p>
                <h2 className="mt-0.5 break-words text-heading3 font-semibold text-foreground">{session.user_problem}</h2>
              </div>
              <Button asChild variant="outline" size="sm">
                <Link href={`/projects/lab/${encodeURIComponent(session.id)}`}>
                  {t("projects.lab.openDetail")}
                  <ExternalLink strokeWidth={1.8} />
                </Link>
              </Button>
            </div>
            <LabClarificationList session={session} mutation={answerQuestion} hasError={answerQuestion.isError} focusQuestionId={focusQuestionId} onAnswer={(questionId, answer) => answerQuestion.mutate({ questionId, answer })} />
            {answerQuestion.isError ? <p role="alert" className="mt-3 text-body2 text-destructive-text">{answerQuestion.error instanceof Error ? answerQuestion.error.message : t("projects.lab.answerFailed")}</p> : null}
            <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-border pt-4">
              <Button type="button" disabled={!workflow?.canGenerate || generateSolution.isPending} onClick={() => generateSolution.mutate()}>
                {generateSolution.isPending ? <><Loader2 className="animate-spin" strokeWidth={1.8} /> {t("projects.lab.generating")}</> : <>{t("projects.lab.generateSolution")} <ChevronRight strokeWidth={1.8} /></>}
              </Button>
              <span className="text-body2 text-muted-foreground" aria-live="polite">{workflow?.actionLabel}</span>
            </div>
            {generateSolution.isError ? (
              <p role="alert" className="mt-3 text-body2 text-destructive-text">
                {generateSolution.error instanceof Error ? generateSolution.error.message : t("projects.lab.solutionFailed")}
                {generateSolution.error && typeof generateSolution.error === "object" && "status" in generateSolution.error && generateSolution.error.status === 409 ? ` ${t("projects.lab.returnToClarification")}` : ""}
              </p>
            ) : null}
            {solution ? <LabSolutionPanel session={solution.session} solution={solution.solution} /> : session.generated_solution || session.solution_json ? <LabSolutionPanel session={session} solution={session.solution_json ?? { markdown: session.generated_solution }} /> : null}
          </section>
          <LabContextPanel session={session} />
        </div>
      )}
    </section>
  )
}

export function LabWorkspaceSkeleton() {
  const { t } = useI18n()
  const block = "rounded-md bg-muted"
  return (
    <section className="space-y-4" aria-label={t("projects.lab.loadingWorkspace")} aria-busy="true">
      <div className="animate-pulse rounded-xl border border-border bg-card p-4">
        <div className={cn(block, "h-3 w-24")} />
        <div className={cn(block, "mt-3 h-6 w-52")} />
        <div className={cn(block, "mt-4 h-28 w-full rounded-lg")} />
        <div className={cn(block, "mt-3 h-9 w-36 rounded-lg")} />
      </div>
      <div className="grid gap-4 lg:grid-cols-[minmax(13rem,0.8fr)_minmax(0,2fr)_minmax(16rem,1fr)]">
        <div className="animate-pulse rounded-xl border border-border bg-card p-4">
          <div className={cn(block, "h-3 w-20")} />
          <div className={cn(block, "mt-3 h-5 w-36")} />
          <div className={cn(block, "mt-5 h-16 rounded-lg")} />
        </div>
        <div className="animate-pulse space-y-4 rounded-xl border border-border bg-card p-4">
          <div className={cn(block, "h-5 w-2/3")} />
          <div className={cn(block, "h-28 rounded-lg")} />
          <div className={cn(block, "h-28 rounded-lg")} />
        </div>
        <div className="animate-pulse rounded-xl border border-border bg-card p-4">
          <div className={cn(block, "h-3 w-16")} />
          <div className={cn(block, "mt-3 h-5 w-44")} />
          <div className={cn(block, "mt-5 h-44 rounded-lg")} />
        </div>
      </div>
    </section>
  )
}

function ProjectGrid({ projects, meta }: { projects: ProjectsApiProject[]; meta: ProjectsApiMeta }) {
  if (!projects.length) return <ProjectEmptyState />
  return (
    <div className="space-y-3">
      <ProjectSourceLine meta={meta} />
      <div className={projectGridClassName}>
        {projects.map((project) => (
          <ProjectProductCard key={project.id} project={project} />
        ))}
      </div>
    </div>
  )
}

function SectionHeader({ title, href }: { title: string; href: string }) {
  return (
    <ProjectSectionHeading
      title={title}
      action={
        <Button asChild variant="ghost" size="sm">
          <Link href={href}>
            Open
            <ArrowRight strokeWidth={1.8} />
          </Link>
        </Button>
      }
    />
  )
}

function SimplePanel({ title, subtitle, body, href }: { title: string; subtitle: string; body: string; href?: string }) {
  const content = (
    <article className={projectCardClassName}>
      <p className="text-caption uppercase text-muted-foreground">{subtitle}</p>
      <h3 className="mt-1 break-words text-[15px] font-semibold text-foreground">{title}</h3>
      <p className="mt-1 line-clamp-3 text-body2 text-muted-foreground">{body || "No additional text was available from the real Project Radar artifact."}</p>
    </article>
  )
  return href ? (
    <Link href={href} className={projectCardLinkClassName}>
      {content}
    </Link>
  ) : (
    content
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
  return (
    <article className={projectCardClassName}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate font-mono text-label text-faint-foreground">{item.project_id}</p>
          <p className="mt-1 break-words text-body2 font-semibold text-foreground">{item.watch_reason}</p>
        </div>
        <span className={cn("shrink-0 text-label font-semibold", toneTextClass(levelTone(item.priority)))}>{item.priority}</span>
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        <Badge variant={item.status === "active" ? "info" : "muted"}>{item.status}</Badge>
        {watchTopics.map((topic) => <Badge key={topic} variant="muted">{topic}</Badge>)}
      </div>
      {lastChangeSummary ? <p className="mt-2 text-label text-muted-foreground">{lastChangeSummary}</p> : null}
      {signals.length > 0 && (
        <div className="mt-3 border-t border-border pt-3">
          <p className="mb-2 text-caption uppercase text-muted-foreground">Signals</p>
          <ul className="space-y-2">
            {signals.map((signal, index) => (
              <li key={textValue(signal.id) || index} className="flex items-start gap-2">
                <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", toneDotClass(levelTone(signal.severity)))} aria-hidden="true" />
                <div className="min-w-0">
                  <p className="text-label font-medium text-foreground">{textValue(signal.title)}</p>
                  <p className="text-label text-muted-foreground">{textValue(signal.summary)}</p>
                  {textValue(signal.source_url) ? (
                    <a href={textValue(signal.source_url)} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 rounded-sm text-label text-link outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50">
                      source
                      <ExternalLink size={12} strokeWidth={1.8} aria-hidden="true" />
                    </a>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </article>
  )
}

const labNoticeClassName = "mt-4 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-body2 text-warning-text"

export function LabWorkflowStatus({ session }: { session: ProjectsLabSession }) {
  const { t } = useI18n()
  const workflow = presentLabWorkflow(session)
  const tone = workflowTone(workflow.statusTone)
  const stageLabel = workflow.isUnknown ? `${t("projects.lab.unsupportedStageLabel")}: ${session.raw_current_stage ?? "unknown"}` : t(`projects.lab.stage.${session.current_stage}`)
  const actionLabel = workflow.isUnknown ? t("projects.lab.actionsDisabled") : t(`projects.lab.action.${session.next_action}`)
  const StatusIcon = workflow.statusTone === "success" ? Check : workflow.statusTone === "warning" ? AlertCircle : CircleHelp
  return (
    <aside className="h-fit min-w-0 rounded-xl border border-border bg-card p-4" aria-label={t("projects.lab.workflow")}>
      <div className="flex items-start gap-3">
        <span className={cn("mt-0.5 grid size-9 shrink-0 place-items-center rounded-full", toneSurfaceClass(tone))} aria-hidden="true">
          <StatusIcon size={16} strokeWidth={1.8} />
        </span>
        <div className="min-w-0">
          <p className="text-label text-muted-foreground">{t("projects.lab.workflow")}</p>
          <h2 className="mt-0.5 text-body1 font-semibold text-foreground">{stageLabel}</h2>
          <p className="mt-1 text-body2 text-muted-foreground" aria-live="polite">{actionLabel}</p>
        </div>
      </div>
      {workflow.unansweredCount > 0 ? <p className={labNoticeClassName}>{t("projects.lab.remaining", { count: workflow.unansweredCount, suffix: workflow.unansweredCount === 1 ? "" : "s" })}</p> : null}
      {workflow.isUnknown ? <p className={labNoticeClassName}>{t("projects.lab.actionsDisabled")}</p> : null}
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
  if (!session.questions.length) return <p className="mt-4 text-body2 text-muted-foreground">{t("projects.lab.noQuestions")}</p>
  return (
    <div className="mt-4 space-y-2" aria-label={t("projects.lab.questions")}>
      {session.questions.map((question, index) => {
        const answered = !unanswered.has(question.id) || labQuestionAnswered(question.answered_value)
        const draft = drafts[question.id] ?? ""
        const active = !answered && question.id === firstUnansweredId
        return (
          <div key={question.id} className={cn("rounded-xl border p-4", answered ? "border-border bg-muted/50" : active ? "border-ring/60 bg-card" : "border-border bg-card")}>
            <div className="flex items-start gap-3">
              <span className="mt-0.5 grid size-6 shrink-0 place-items-center rounded-full bg-muted text-caption font-semibold text-muted-foreground">{index + 1}</span>
              <div className="min-w-0 flex-1">
                <p className="text-body2 font-medium text-foreground">{question.question}</p>
                {answered ? (
                  <p className="mt-2 flex items-center gap-1.5 text-body2 text-muted-foreground">
                    <Check size={14} strokeWidth={1.8} className="text-success-text" aria-hidden="true" />
                    {String(question.answered_value ?? t("projects.lab.answer"))}
                  </p>
                ) : !active ? (
                  <p className="mt-2 text-label text-muted-foreground">{t("projects.lab.waitingPrevious")}</p>
                ) : (
                  <div className="mt-3 space-y-2">
                    {question.options?.length ? (
                      <div className="flex flex-wrap gap-1.5">
                        {question.options.map((option) => (
                          <ProjectFilterChip
                            key={option.value}
                            active={draft === option.value}
                            className="min-h-9"
                            onClick={() => setDrafts((current) => ({ ...current, [question.id]: option.value }))}
                          >
                            {option.label}
                          </ProjectFilterChip>
                        ))}
                      </div>
                    ) : null}
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
                      <Button type="button" variant="outline" size="lg" className="shrink-0" disabled={!draft.trim() || mutation.isPending} onClick={() => onAnswer(question.id, draft.trim())}>
                        {mutation.isPending ? <><Loader2 className="animate-spin" strokeWidth={1.8} /> {t("projects.lab.savingAnswer")}</> : t("projects.lab.answer")}
                      </Button>
                    </div>
                    {hasError ? <p id={`lab-question-${question.id}-error`} className="text-body2 text-destructive-text" role="alert">{t("projects.lab.answerError")}</p> : null}
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
    <aside className="h-fit min-w-0 space-y-4 rounded-xl border border-border bg-card p-4">
      <div>
        <p className="text-label text-muted-foreground">{t("projects.lab.context")}</p>
        <h2 className="mt-0.5 text-body1 font-semibold text-foreground">{t("projects.lab.references")}</h2>
      </div>
      <div className={cn(projectInsetClassName, "text-body2")}>
        <p className="font-medium text-foreground">{t("projects.lab.selectedCases", { count: session.selected_case_ids.length, suffix: session.selected_case_ids.length === 1 ? "" : "s" })}</p>
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
  if (!record) return <p className="text-body2 text-muted-foreground">{t("projects.lab.graphUnavailable")}</p>
  if (!nodes.length) return <p className="text-body2 text-muted-foreground">{t("projects.lab.noGraphNodes")}</p>
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
        <h3 id="lab-graph-title" className="text-body2 font-semibold text-foreground">{t("projects.lab.graphContext")}</h3>
        <span className="text-label text-muted-foreground">{t("projects.lab.graphSummary", { nodes: nodes.length, edges: edges.length })}</span>
      </div>
      <div className="mt-3 overflow-hidden rounded-lg bg-muted p-2 text-muted-foreground">
        <svg viewBox="0 0 400 240" className="w-full" role="img" aria-label={t("projects.lab.graphAria", { nodes: nodes.length, edges: edges.length })} style={{ maxHeight: 200 }}>
          {edges.map((edge, index) => {
            const source = positions[textValue(edge.source_id)]
            const target = positions[textValue(edge.target_id)]
            return source && target ? <line key={index} x1={source.x} y1={source.y} x2={target.x} y2={target.y} stroke={labGraphEdgeColor} strokeWidth={1.5} /> : null
          })}
          {nodes.map((node) => {
            const id = textValue(node.id)
            const position = positions[id]
            if (!position) return null
            const active = focused.includes(id) || selectedNode === id
            return (
              <g key={id}>
                <circle cx={position.x} cy={position.y} r={active ? 10 : 7} fill={labNodeColor(textValue(node.node_type))} opacity={active ? 1 : 0.7} />
                <text x={position.x} y={position.y + 18} textAnchor="middle" fontSize={8} fill="currentColor">{textValue(node.title).slice(0, 18)}</text>
              </g>
            )
          })}
        </svg>
      </div>
      <ol className="mt-3 space-y-1" aria-label={t("projects.lab.graphNodesRelationships")}>
        {nodes.map((node) => {
          const id = textValue(node.id)
          const related = edges.filter((edge) => textValue(edge.source_id) === id || textValue(edge.target_id) === id).length
          return (
            <li key={id} className="flex items-center gap-1">
              <button
                type="button"
                className="flex min-h-10 min-w-0 flex-1 items-center gap-2 rounded-lg px-2 text-start text-body2 outline-none transition-colors hover:bg-accent/60 focus-visible:ring-3 focus-visible:ring-ring/50 aria-pressed:bg-accent"
                aria-pressed={selectedNode === id}
                onClick={() => setSelectedNode(id)}
              >
                <span className="size-2 shrink-0 rounded-full" style={{ backgroundColor: labNodeColor(textValue(node.node_type)) }} aria-hidden="true" />
                <span className="min-w-0 truncate font-medium text-foreground">{textValue(node.title) || t("projects.lab.unnamedNode")}</span>
                <span className="shrink-0 text-label text-muted-foreground">{textValue(node.node_type)} · {t("projects.lab.related", { count: related })}</span>
              </button>
              <Button type="button" variant="ghost" size="icon-sm" aria-label={t("projects.lab.explain", { title: textValue(node.title) })} disabled={explain.isPending} onClick={() => explain.mutate(id)}>
                <Info strokeWidth={1.8} />
              </Button>
            </li>
          )
        })}
      </ol>
      {explain.isPending ? <p className="mt-3 text-body2 text-muted-foreground" aria-live="polite">{t("projects.lab.loadingExplanation")}</p> : null}
      {error ? <p className="mt-3 text-body2 text-destructive-text" role="alert">{error}</p> : null}
      {explanation ? (
        <div className="mt-3 rounded-lg border border-link/30 bg-link/10 p-3" aria-live="polite">
          <p className="text-body2 font-semibold text-foreground">{explanation.title}</p>
          <p className="mt-1 text-body2 text-muted-foreground">{explanation.explanation}</p>
          {explanation.related_nodes.length ? <p className="mt-2 text-label text-muted-foreground">{explanation.related_nodes.length} related node{explanation.related_nodes.length === 1 ? "" : "s"}</p> : null}
        </div>
      ) : null}
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
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-label text-muted-foreground">{t("projects.lab.output")}</p>
          <h2 id="lab-solution-title" className="mt-0.5 text-body1 font-semibold text-foreground">{t("projects.lab.generatedSolution")}</h2>
        </div>
        <Button asChild variant="outline" size="sm">
          <Link href={`/projects/lab/${encodeURIComponent(session.id)}`}>
            {t("projects.lab.openSessionDetail")}
            <ExternalLink strokeWidth={1.8} />
          </Link>
        </Button>
      </div>
      <Tabs defaultValue="summary" className="mt-4">
        <TabsList className="max-w-full overflow-x-auto">
          <TabsTrigger value="summary">{t("projects.lab.summary")}</TabsTrigger>
          <TabsTrigger value="structured">{t("projects.lab.structured")}</TabsTrigger>
          <TabsTrigger value="evidence">{t("projects.lab.evidence")}</TabsTrigger>
        </TabsList>
        <TabsContent value="summary">
          <div className="rounded-xl border border-border bg-muted/50 p-4">
            <p className="whitespace-pre-wrap text-body2 text-foreground">{summaryText}</p>
            {textValue(evidence?.module) ? <p className="mt-3 text-body2 font-medium text-foreground">Module: {textValue(evidence?.module)}</p> : null}
            <p className="mt-3 text-label text-muted-foreground">Structured fields: {structuredKeys.length ? structuredKeys.join(", ") : "Unavailable"}</p>
          </div>
        </TabsContent>
        <TabsContent value="structured">
          <div className="relative">
            <ProjectCodeBlock className="max-h-80 pe-14">{JSON.stringify(structured, null, 2)}</ProjectCodeBlock>
            <Button type="button" variant="outline" size="icon-sm" className="absolute end-2 top-2 bg-background" aria-label={t("projects.lab.copyStructured")} title={t("projects.lab.copyStructured")} onClick={copyStructured}>
              <Clipboard strokeWidth={1.8} />
            </Button>
          </div>
          <p className="mt-2 text-label text-muted-foreground" aria-live="polite">
            {copyState === "success" ? t("projects.lab.copySuccess") : copyState === "error" ? t("projects.lab.copyError") : t("projects.lab.copyHint")}
          </p>
        </TabsContent>
        <TabsContent value="evidence">
          <dl className="grid gap-2 sm:grid-cols-2">
            {evidenceKeys.map((key) => (
              <div key={key} className={projectInsetClassName}>
                <dt className="text-caption uppercase text-muted-foreground">{key.replaceAll("_", " ")}</dt>
                <dd className="mt-1 break-words text-body2 text-foreground">
                  {evidence?.[key] === undefined ? "Unavailable from the API response." : Array.isArray(evidence[key]) ? evidence[key].join(", ") : String(evidence[key])}
                </dd>
              </div>
            ))}
          </dl>
        </TabsContent>
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
