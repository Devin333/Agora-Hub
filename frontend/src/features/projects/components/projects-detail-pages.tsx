"use client"

import Link from "next/link"
import { ArrowLeft, ExternalLink, Loader2 } from "lucide-react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import {
  explainProjectCase,
  fetchProjectCaseDetail,
  fetchProjectCollectionDetail,
  fetchProjectLabSession,
  fetchProjectToolDetail,
  fetchProjectV1Detail,
  mapProjectCaseToContext,
  saveProjectLabSession,
} from "@/lib/projects/api"
import type { ProjectsApiCollection, ProjectsApiProjectDetail, ProjectsApiTool } from "@/types/projects"
import { ProjectEmptyState, ProjectErrorState, ProjectLoadingState, ProjectSourceLine } from "@/features/projects/components/project-product-state"
import { formatScore, labelize } from "@/features/projects/components/project-format"
import { LabGraph, LabSolutionPanel, LabWorkflowStatus } from "@/features/projects/components/projects-product-page"
import { presentLabWorkflow, labSolutionValue } from "@/features/projects/components/lab-workflow"
import { useI18n } from "@/lib/i18n/use-i18n"

type UnknownRecord = Record<string, unknown>

export function ProjectV1DetailPage({ projectId }: { projectId: string }) {
  const query = useQuery({
    queryKey: ["projects", "v1-detail", projectId],
    queryFn: () => fetchProjectV1Detail(projectId),
  })
  if (query.isLoading) return <ProjectLoadingState title="Loading Project" />
  if (query.isError) return <ProjectErrorState message={query.error instanceof Error ? query.error.message : undefined} onRetry={() => query.refetch()} />
  if (!query.data) return <ProjectEmptyState />
  return <ProjectV1Detail data={query.data} />
}

export function ProjectToolDetailPage({ projectId }: { projectId: string }) {
  const query = useQuery({
    queryKey: ["projects", "tool-detail", projectId],
    queryFn: () => fetchProjectToolDetail(projectId),
  })
  if (query.isLoading) return <ProjectLoadingState title="Loading Tool" />
  if (query.isError) return <ProjectErrorState message={query.error instanceof Error ? query.error.message : undefined} onRetry={() => query.refetch()} />
  if (!query.data) return <ProjectEmptyState />
  return <ToolDetail tool={query.data} />
}

export function ProjectCaseDetailPage({ caseId }: { caseId: string }) {
  const [context, setContext] = useState("")
  const detail = useQuery({
    queryKey: ["projects", "case-detail", caseId],
    queryFn: () => fetchProjectCaseDetail(caseId),
  })
  const explain = useMutation({
    mutationFn: () => explainProjectCase(caseId, { style: "migration", user_context: context.trim() || undefined }),
  })
  const mapping = useMutation({
    mutationFn: () => mapProjectCaseToContext(caseId, { user_context: context.trim(), constraints: [] }),
  })

  if (detail.isLoading) return <ProjectLoadingState title="Loading Case" />
  if (detail.isError) return <ProjectErrorState message={detail.error instanceof Error ? detail.error.message : undefined} onRetry={() => detail.refetch()} />
  if (!detail.data) return <ProjectEmptyState />
  const item = detail.data
  return (
    <main className="space-y-6">
      <BackLink href="/projects/cases" label="Cases" />
      <Header title={item.title} eyebrow={`${item.business_domain} / ${item.module_type}`} body={String(item.design_summary ?? item.problem ?? "")} />
      <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="space-y-4">
          <Panel title="Problem" body={String(item.problem ?? "")} />
          <ComponentsPanel components={item.components} />
          <PatternsPanel patterns={item.patterns} />
        </div>
        <aside className="space-y-3 rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
          <h2 className="text-base font-semibold text-[#202124] dark:text-foreground">Apply Context</h2>
          <textarea
            value={context}
            onChange={(event) => setContext(event.target.value)}
            className="min-h-28 w-full rounded-md border border-input bg-card px-3 py-2 text-sm"
            placeholder="Target module context"
          />
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" onClick={() => explain.mutate()} disabled={explain.isPending}>
              Explain
            </Button>
            <Button type="button" onClick={() => mapping.mutate()} disabled={!context.trim() || mapping.isPending}>
              Map
            </Button>
          </div>
          {explain.data ? <Panel title="Explanation" body={explain.data.summary} /> : null}
          {mapping.data ? <JsonPanel title={`Fit ${formatScore(mapping.data.fit_score)}`} value={mapping.data} /> : null}
        </aside>
      </section>
    </main>
  )
}

export function ProjectCollectionDetailPage({ slug }: { slug: string }) {
  const query = useQuery({
    queryKey: ["projects", "collection-detail", slug],
    queryFn: () => fetchProjectCollectionDetail(slug),
  })
  if (query.isLoading) return <ProjectLoadingState title="Loading Collection" />
  if (query.isError) return <ProjectErrorState message={query.error instanceof Error ? query.error.message : undefined} onRetry={() => query.refetch()} />
  if (!query.data) return <ProjectEmptyState />
  return <CollectionDetail collection={query.data} />
}

export function ProjectLabSessionPage({ sessionId }: { sessionId: string }) {
  const { t } = useI18n()
  const [savedMessage, setSavedMessage] = useState<string | null>(null)
  const saveStatusRef = useRef<HTMLParagraphElement>(null)
  const query = useQuery({
    queryKey: ["projects", "lab-session", sessionId],
    queryFn: () => fetchProjectLabSession(sessionId),
  })
  const save = useMutation({
    mutationFn: () => saveProjectLabSession(sessionId, { status: "saved", note: "Saved from Projects Lab detail." }),
    onSuccess: (result) => {
      setSavedMessage(result.session.current_stage === "solution_adopted" ? t("projects.lab.solutionAdopted") : result.session.current_stage === "solution_archived" ? t("projects.lab.sessionArchived") : t("projects.lab.sessionSaved"))
      void query.refetch()
    },
  })
  useEffect(() => {
    if (savedMessage) saveStatusRef.current?.focus()
  }, [savedMessage])
  if (query.isLoading) return <ProjectLoadingState title="Loading Lab Session" />
  if (query.isError) return <ProjectErrorState message={query.error instanceof Error ? query.error.message : undefined} onRetry={() => query.refetch()} />
  if (!query.data) return <ProjectEmptyState />
  const session = query.data.session
  const workflow = presentLabWorkflow(session)
  const solution = labSolutionValue(session) ?? (session.generated_solution ? { markdown: session.generated_solution } : null)
  const canSave = !workflow.isUnknown && Boolean(solution) && ["solution_generated"].includes(session.current_stage)
  return (
    <main className="space-y-6 overflow-x-hidden">
      <BackLink href="/projects/lab" label="Lab" />
      <Header title="Lab Session" eyebrow={session.current_stage} body={session.user_problem} />
      <div className="flex flex-wrap items-center gap-3">
        <LabWorkflowStatus session={session} />
        <Button
          type="button"
          onClick={() => save.mutate()}
          disabled={save.isPending || !canSave}
          aria-busy={save.isPending}
        >
          {save.isPending ? <><Loader2 className="size-4 animate-spin" /> {t("projects.lab.savingSession")}</> : t("projects.lab.saveSession")}
        </Button>
        {!canSave && !workflow.isUnknown && session.current_stage !== "solution_saved" ? <p className="basis-full text-sm text-muted-foreground">{t("projects.lab.saveAvailable")}</p> : null}
        {workflow.isUnknown ? <p className="basis-full text-sm text-destructive">{t("projects.lab.unsupportedStage")}</p> : null}
        {save.isError ? <div className="flex basis-full flex-wrap items-center gap-3 text-sm text-destructive" role="alert"><span>{save.error instanceof Error ? save.error.message : t("projects.lab.saveFailed")}</span><Button type="button" variant="outline" size="sm" onClick={() => save.mutate()}>{t("projects.lab.retrySave")}</Button></div> : null}
        {savedMessage ? <p ref={saveStatusRef} tabIndex={-1} className="basis-full text-sm text-emerald-700 outline-none focus-visible:ring-2 focus-visible:ring-ring dark:text-emerald-300" aria-live="polite">{savedMessage}</p> : null}
      </div>
      <section className="grid gap-4 lg:grid-cols-2">
        <RequirementProfilePanel profile={session.requirement_profile} />
        <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card"><LabGraph sessionId={session.id} graph={session.graph_state} /></section>
        <QuestionsPanel questions={session.questions} />
        {solution ? <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card"><LabSolutionPanel session={session} solution={solution} /></section> : <SolutionPanel solution={null} />}
      </section>
    </main>
  )
}

function ProjectV1Detail({ data }: { data: ProjectsApiProjectDetail }) {
  const project = data.project
  return (
    <main className="space-y-6">
      <BackLink href="/projects" label="Projects" />
      <Header title={project.name} eyebrow={project.project_type} body={project.description ?? project.tagline ?? ""} />
      <ProjectSourceLine meta={data.meta} />
      <section className="grid gap-4 lg:grid-cols-3">
        <Panel title="Hot score" body={formatScore(project.hot_score ?? undefined)} />
        <Panel title="Rising score" body={formatScore(project.rising_score ?? undefined)} />
        <Panel title="Sources" body={String(project.source_count)} />
      </section>
      <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="space-y-4">
          <CapabilitiesPanel capabilities={data.capabilities} />
          <CasesPanel cases={data.cases} />
          <MetricsPanel metrics={data.metrics} />
        </div>
        <aside className="space-y-4">
          <LinkPanel project={project} />
          <WatchPanel status={data.watch_status} />
          <ActionsPanel actions={data.recommended_actions} />
        </aside>
      </section>
    </main>
  )
}

function ToolDetail({ tool }: { tool: ProjectsApiTool }) {
  return (
    <main className="space-y-6">
      <BackLink href="/projects/tools" label="Tools" />
      <Header title={tool.project.name} eyebrow={tool.profile.tool_type} body={tool.fit_reason ?? tool.project.description ?? ""} />
      <section className="grid gap-4 lg:grid-cols-2">
        <IntegrationProfilePanel profile={tool.profile} />
        <CapabilitiesPanel capabilities={tool.capabilities} />
      </section>
    </main>
  )
}

function CollectionDetail({ collection }: { collection: ProjectsApiCollection }) {
  return (
    <main className="space-y-6">
      <BackLink href="/projects/collections" label="Collections" />
      <Header title={collection.title} eyebrow={`${collection.item_count ?? 0} items`} body={collection.description} />
      <SectionsPanel sections={collection.sections} />
    </main>
  )
}

function BackLink({ href, label }: { href: string; label: string }) {
  return (
    <Button asChild variant="outline" size="sm">
      <Link href={href}>
        <ArrowLeft className="size-4" />
        {label}
      </Link>
    </Button>
  )
}

function Header({ title, eyebrow, body }: { title: string; eyebrow: string; body: string }) {
  return (
    <header className="space-y-4 py-6">
      <p className="text-xs font-semibold uppercase text-[#667085] dark:text-muted-foreground">{labelize(eyebrow)}</p>
      <h1 className="max-w-5xl text-4xl font-black leading-tight tracking-normal text-[#202124] sm:text-5xl dark:text-foreground">{title}</h1>
      {body ? <p className="max-w-3xl text-base leading-7 text-[#4b5563] dark:text-muted-foreground">{body}</p> : null}
    </header>
  )
}

function LinkPanel({ project }: { project: ProjectsApiProjectDetail["project"] }) {
  const url = project.github_url ?? project.website_url ?? project.canonical_url
  return (
    <div className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-base font-semibold text-[#202124] dark:text-foreground">Links</h2>
      {url ? (
        <Button asChild className="mt-3" size="sm">
          <a href={url} target="_blank" rel="noreferrer">
            Open Source
            <ExternalLink className="size-4" />
          </a>
        </Button>
      ) : (
        <p className="mt-2 text-sm text-muted-foreground">No public URL in the current artifact.</p>
      )}
    </div>
  )
}

function Panel({ title, body }: { title: string; body: string }) {
  return (
    <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-sm font-semibold text-[#202124] dark:text-foreground">{title}</h2>
      <p className="mt-2 text-sm leading-6 text-[#4b5563] dark:text-muted-foreground">{body || "Unavailable"}</p>
    </section>
  )
}

function JsonPanel({ title, value }: { title: string; value: unknown }) {
  return (
    <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-sm font-semibold text-[#202124] dark:text-foreground">{title}</h2>
      <pre className="mt-3 max-h-96 overflow-auto rounded-md bg-[#111827] p-3 text-xs leading-5 text-white">{JSON.stringify(value, null, 2)}</pre>
    </section>
  )
}

function CapabilitiesPanel({ capabilities }: { capabilities: unknown[] }) {
  const items = Array.isArray(capabilities) ? capabilities : []
  return (
    <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-sm font-semibold text-[#202124] dark:text-foreground">Capabilities</h2>
      <div className="mt-3 space-y-2">
        {items.length ? (
          items.map((cap, idx) => {
            const record = asRecord(cap)
            const name = textValue(record?.name) || textValue(cap)
            const description = textValue(record?.description)
            return (
            <div key={idx} className="rounded-md bg-[#f8fafc] p-2 dark:bg-background">
              <p className="text-xs font-semibold text-[#202124] dark:text-foreground">{name}</p>
              {description ? <p className="mt-1 text-xs text-[#4b5563] dark:text-muted-foreground">{description}</p> : null}
            </div>
          )})
        ) : (
          <p className="text-xs text-muted-foreground">No capabilities available</p>
        )}
      </div>
    </section>
  )
}

function CasesPanel({ cases }: { cases: unknown[] }) {
  const items = Array.isArray(cases) ? cases : []
  return (
    <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-sm font-semibold text-[#202124] dark:text-foreground">Cases</h2>
      <div className="mt-3 space-y-2">
        {items.length ? (
          items.map((item, idx) => {
            const record = asRecord(item)
            const title = textValue(record?.title) || textValue(item)
            const moduleType = textValue(record?.module_type)
            return (
              <div key={idx} className="rounded-md bg-[#f8fafc] p-2 dark:bg-background">
                <p className="text-xs font-semibold text-[#202124] dark:text-foreground">{title}</p>
                {moduleType ? <p className="text-xs text-[#667085] dark:text-muted-foreground">{moduleType}</p> : null}
              </div>
            )
          })
        ) : (
          <p className="text-xs text-muted-foreground">No cases available</p>
        )}
      </div>
    </section>
  )
}

function MetricsPanel({ metrics }: { metrics: unknown[] }) {
  const m = Array.isArray(metrics) && metrics.length > 0 ? asRecord(metrics[0]) : null
  if (!m) return null
  const fields: [string, string | number][] = [
    ["Stars", displayValue(m.github_stars)],
    ["Forks", displayValue(m.github_forks)],
    ["Quality", numericValue(m.quality_score) !== undefined ? formatScore(numericValue(m.quality_score)) : undefined],
    ["Activity", numericValue(m.activity_score) !== undefined ? formatScore(numericValue(m.activity_score)) : undefined],
    ["Evidence", numericValue(m.evidence_score) !== undefined ? formatScore(numericValue(m.evidence_score)) : undefined],
    ["Mentions", displayValue(m.source_mentions)],
  ].filter((field): field is [string, string | number] => field[1] !== undefined && field[1] !== null)
  return (
    <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-sm font-semibold text-[#202124] dark:text-foreground">Metrics</h2>
      <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
        {fields.map(([label, value]) => (
          <div key={label} className="rounded-md bg-[#f8fafc] p-2 dark:bg-background">
            <p className="text-[#667085] dark:text-muted-foreground">{label}</p>
            <p className="font-semibold text-[#202124] dark:text-foreground">{value}</p>
          </div>
        ))}
      </div>
    </section>
  )
}

function WatchPanel({ status }: { status?: unknown }) {
  const watchStatus = textValue(asRecord(status)?.status) || "not_watched"
  return (
    <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-sm font-semibold text-[#202124] dark:text-foreground">Watch Status</h2>
      <p className="mt-2 text-xs text-[#4b5563] dark:text-muted-foreground">{watchStatus}</p>
    </section>
  )
}

function ActionsPanel({ actions }: { actions?: unknown }) {
  const items = Array.isArray(actions) ? actions : []
  if (!items.length) return null
  return (
    <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-sm font-semibold text-[#202124] dark:text-foreground">Recommended</h2>
      <div className="mt-2 space-y-1">
        {items.map((action, idx) => {
          const title = textValue(asRecord(action)?.title) || textValue(action)
          return <p key={idx} className="text-xs text-[#4b5563] dark:text-muted-foreground">• {title}</p>
        })}
      </div>
    </section>
  )
}

function IntegrationProfilePanel({ profile }: { profile: ProjectsApiTool["profile"] }) {
  return (
    <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-sm font-semibold text-[#202124] dark:text-foreground">Integration Profile</h2>
      <div className="mt-3 space-y-2 text-xs">
        <div className="flex justify-between">
          <span className="text-[#667085] dark:text-muted-foreground">Type</span>
          <span className="font-semibold text-[#202124] dark:text-foreground">{profile.tool_type}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-[#667085] dark:text-muted-foreground">Difficulty</span>
          <span className="font-semibold text-[#202124] dark:text-foreground">{profile.integration_difficulty}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-[#667085] dark:text-muted-foreground">Has API</span>
          <span className="font-semibold text-[#202124] dark:text-foreground">{profile.has_api ? "Yes" : "No"}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-[#667085] dark:text-muted-foreground">Has CLI</span>
          <span className="font-semibold text-[#202124] dark:text-foreground">{profile.has_cli ? "Yes" : "No"}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-[#667085] dark:text-muted-foreground">Python SDK</span>
          <span className="font-semibold text-[#202124] dark:text-foreground">{profile.has_python_sdk ? "Yes" : "No"}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-[#667085] dark:text-muted-foreground">Docker</span>
          <span className="font-semibold text-[#202124] dark:text-foreground">{profile.has_docker ? "Yes" : "No"}</span>
        </div>
      </div>
    </section>
  )
}

function ComponentsPanel({ components }: { components?: unknown }) {
  const items = Array.isArray(components) ? components : []
  return (
    <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-sm font-semibold text-[#202124] dark:text-foreground">Components</h2>
      <div className="mt-3 space-y-2">
        {items.length ? (
          items.map((comp, idx) => {
            const record = asRecord(comp)
            return (
              <div key={idx} className="rounded-md bg-[#f8fafc] p-2 dark:bg-background">
                <p className="text-xs font-semibold text-[#202124] dark:text-foreground">{textValue(record?.name) || "Unnamed component"}</p>
                {record?.component_type ? <p className="text-xs text-[#667085] dark:text-muted-foreground">{textValue(record.component_type)}</p> : null}
                {record?.responsibility ? <p className="mt-1 text-xs text-[#4b5563] dark:text-muted-foreground">{textValue(record.responsibility)}</p> : null}
              </div>
            )
          })
        ) : (
          <p className="text-xs text-muted-foreground">No components available</p>
        )}
      </div>
    </section>
  )
}

function PatternsPanel({ patterns }: { patterns?: unknown }) {
  const items = Array.isArray(patterns) ? patterns : []
  return (
    <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-sm font-semibold text-[#202124] dark:text-foreground">Patterns</h2>
      <div className="mt-3 space-y-2">
        {items.length ? (
          items.map((pat, idx) => {
            const record = asRecord(pat)
            return (
              <div key={idx} className="rounded-md bg-[#f8fafc] p-2 dark:bg-background">
                <p className="text-xs font-semibold text-[#202124] dark:text-foreground">{textValue(record?.name) || "Unnamed pattern"}</p>
                {record?.pattern_type ? <p className="text-xs text-[#667085] dark:text-muted-foreground">{textValue(record.pattern_type)}</p> : null}
                {record?.explanation ? <p className="mt-1 text-xs text-[#4b5563] dark:text-muted-foreground">{textValue(record.explanation)}</p> : null}
              </div>
            )
          })
        ) : (
          <p className="text-xs text-muted-foreground">No patterns available</p>
        )}
      </div>
    </section>
  )
}

function SectionsPanel({ sections }: { sections?: unknown }) {
  const items = Array.isArray(sections) ? sections : []
  return (
    <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-sm font-semibold text-[#202124] dark:text-foreground">Sections</h2>
      <div className="mt-3 space-y-3">
        {items.length ? (
          items.map((sec, idx) => {
            const record = asRecord(sec)
            const sectionItems = Array.isArray(record?.items) ? record.items : []
            return (
              <div key={idx} className="rounded-md bg-[#f8fafc] p-3 dark:bg-background">
                <p className="text-xs font-semibold text-[#202124] dark:text-foreground">{textValue(record?.title) || "Untitled section"}</p>
                {sectionItems.length ? (
                  <div className="mt-2 space-y-1">
                    {sectionItems.map((item, i) => (
                      <p key={i} className="text-xs text-[#4b5563] dark:text-muted-foreground">• {textValue(asRecord(item)?.title) || textValue(item)}</p>
                    ))}
                  </div>
                ) : null}
              </div>
            )
          })
        ) : (
          <p className="text-xs text-muted-foreground">No sections available</p>
        )}
      </div>
    </section>
  )
}

function RequirementProfilePanel({ profile }: { profile?: unknown }) {
  const record = asRecord(profile)
  if (!record) return null
  return (
    <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-sm font-semibold text-[#202124] dark:text-foreground">Requirement Profile</h2>
      <div className="mt-3 space-y-1 text-xs">
        {Object.entries(record).map(([key, value]) => (
          <div key={key} className="flex justify-between">
            <span className="text-[#667085] dark:text-muted-foreground">{key}</span>
            <span className="font-semibold text-[#202124] dark:text-foreground">{String(value)}</span>
          </div>
        ))}
      </div>
    </section>
  )
}

function QuestionsPanel({ questions }: { questions?: unknown }) {
  const items = Array.isArray(questions) ? questions : []
  return (
    <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-sm font-semibold text-[#202124] dark:text-foreground">Questions</h2>
      <div className="mt-3 space-y-2">
        {items.length ? (
          items.map((question, idx) => {
            const record = asRecord(question)
            const answeredValue = record?.answered_value
            return (
              <div key={idx} className="rounded-md bg-[#f8fafc] p-2 dark:bg-background">
                <p className="text-xs font-semibold text-[#202124] dark:text-foreground">Q{idx + 1}</p>
                <p className="mt-1 text-xs text-[#4b5563] dark:text-muted-foreground">{textValue(record?.question)}</p>
                {answeredValue ? <p className="mt-1 text-xs text-[#667085] dark:text-muted-foreground">Answer: {String(answeredValue)}</p> : null}
              </div>
            )
          })
        ) : (
          <p className="text-xs text-muted-foreground">No questions available</p>
        )}
      </div>
    </section>
  )
}

function SolutionPanel({ solution }: { solution?: unknown }) {
  if (!solution) return null
  const text = typeof solution === "string" ? solution : JSON.stringify(solution, null, 2)
  return (
    <section className="rounded-md border border-[#d8dee7] bg-white p-4 shadow-sm dark:border-border dark:bg-card">
      <h2 className="text-sm font-semibold text-[#202124] dark:text-foreground">Solution</h2>
      <pre className="mt-3 max-h-96 overflow-auto rounded-md bg-[#111827] p-3 text-xs leading-5 text-white whitespace-pre-wrap break-words">{text}</pre>
    </section>
  )
}

function asRecord(value: unknown): UnknownRecord | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as UnknownRecord : null
}

function textValue(value: unknown): string {
  if (typeof value === "string") {
    return value
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value)
  }
  return ""
}

function displayValue(value: unknown): string | number | undefined {
  return typeof value === "string" || typeof value === "number" ? value : undefined
}

function numericValue(value: unknown): number | undefined {
  return typeof value === "number" ? value : undefined
}
