"use client"

import Link from "next/link"
import type { ReactNode } from "react"
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
import {
  ProjectCodeBlock,
  ProjectMetric,
  ProjectPage,
  ProjectPageBody,
  ProjectPageHeader,
  ProjectPanel,
  projectInsetClassName,
} from "@/features/projects/components/project-page-layout"
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
    <ProjectPage>
      <DetailHeader backHref="/projects/cases" backLabel="Cases" title={item.title} eyebrow={`${item.business_domain} / ${item.module_type}`} body={String(item.design_summary ?? item.problem ?? "")} />
      <ProjectPageBody>
        <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
          <div className="min-w-0 space-y-4">
            <Panel title="Problem" body={String(item.problem ?? "")} />
            <ComponentsPanel components={item.components} />
            <PatternsPanel patterns={item.patterns} />
          </div>
          <ProjectPanel title="Apply Context" className="h-fit space-y-3">
            <label className="sr-only" htmlFor="project-case-context">
              Target module context
            </label>
            <textarea
              id="project-case-context"
              value={context}
              onChange={(event) => setContext(event.target.value)}
              className={textareaClassName}
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
          </ProjectPanel>
        </section>
      </ProjectPageBody>
    </ProjectPage>
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
    <ProjectPage className="overflow-x-hidden">
      <DetailHeader
        backHref="/projects/lab"
        backLabel="Lab"
        title="Lab Session"
        eyebrow={session.current_stage}
        body={session.user_problem}
        actions={
          <Button type="button" onClick={() => save.mutate()} disabled={save.isPending || !canSave} aria-busy={save.isPending}>
            {save.isPending ? <><Loader2 className="size-4 animate-spin" /> {t("projects.lab.savingSession")}</> : t("projects.lab.saveSession")}
          </Button>
        }
      >
        {!canSave && !workflow.isUnknown && session.current_stage !== "solution_saved" ? <p className="text-body2 text-muted-foreground">{t("projects.lab.saveAvailable")}</p> : null}
        {workflow.isUnknown ? <p className="text-body2 text-destructive-text">{t("projects.lab.unsupportedStage")}</p> : null}
        {save.isError ? (
          <div className="flex flex-wrap items-center gap-3 text-body2 text-destructive-text" role="alert">
            <span>{save.error instanceof Error ? save.error.message : t("projects.lab.saveFailed")}</span>
            <Button type="button" variant="outline" size="sm" onClick={() => save.mutate()}>
              {t("projects.lab.retrySave")}
            </Button>
          </div>
        ) : null}
        {savedMessage ? (
          <p ref={saveStatusRef} tabIndex={-1} className="rounded-sm text-body2 text-success-text outline-none focus-visible:ring-3 focus-visible:ring-ring/50" aria-live="polite">
            {savedMessage}
          </p>
        ) : null}
      </DetailHeader>
      <ProjectPageBody>
        <LabWorkflowStatus session={session} />
        <section className="grid gap-4 lg:grid-cols-2">
          <RequirementProfilePanel profile={session.requirement_profile} />
          <ProjectPanel>
            <LabGraph sessionId={session.id} graph={session.graph_state} />
          </ProjectPanel>
          <QuestionsPanel questions={session.questions} />
          {solution ? (
            <ProjectPanel>
              <LabSolutionPanel session={session} solution={solution} />
            </ProjectPanel>
          ) : (
            <SolutionPanel solution={null} />
          )}
        </section>
      </ProjectPageBody>
    </ProjectPage>
  )
}

function ProjectV1Detail({ data }: { data: ProjectsApiProjectDetail }) {
  const project = data.project
  return (
    <ProjectPage>
      <DetailHeader backHref="/projects" backLabel="Projects" title={project.name} eyebrow={project.project_type} body={project.description ?? project.tagline ?? ""}>
        <ProjectSourceLine meta={data.meta} />
      </DetailHeader>
      <ProjectPageBody>
        <section className="grid gap-2 sm:grid-cols-3">
          <ProjectMetric size="lg" label="Hot score" value={formatScore(project.hot_score ?? undefined)} />
          <ProjectMetric size="lg" label="Rising score" value={formatScore(project.rising_score ?? undefined)} />
          <ProjectMetric size="lg" label="Sources" value={String(project.source_count)} />
        </section>
        <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
          <div className="min-w-0 space-y-4">
            <CapabilitiesPanel capabilities={data.capabilities} />
            <CasesPanel cases={data.cases} />
            <MetricsPanel metrics={data.metrics} />
          </div>
          <aside className="min-w-0 space-y-4">
            <LinkPanel project={project} />
            <WatchPanel status={data.watch_status} />
            <ActionsPanel actions={data.recommended_actions} />
          </aside>
        </section>
      </ProjectPageBody>
    </ProjectPage>
  )
}

function ToolDetail({ tool }: { tool: ProjectsApiTool }) {
  return (
    <ProjectPage>
      <DetailHeader backHref="/projects/tools" backLabel="Tools" title={tool.project.name} eyebrow={tool.profile.tool_type} body={tool.fit_reason ?? tool.project.description ?? ""} />
      <ProjectPageBody>
        <section className="grid gap-4 lg:grid-cols-2">
          <IntegrationProfilePanel profile={tool.profile} />
          <CapabilitiesPanel capabilities={tool.capabilities} />
        </section>
      </ProjectPageBody>
    </ProjectPage>
  )
}

function CollectionDetail({ collection }: { collection: ProjectsApiCollection }) {
  return (
    <ProjectPage>
      <DetailHeader backHref="/projects/collections" backLabel="Collections" title={collection.title} eyebrow={`${collection.item_count ?? 0} items`} body={collection.description} />
      <ProjectPageBody>
        <SectionsPanel sections={collection.sections} />
      </ProjectPageBody>
    </ProjectPage>
  )
}
const textareaClassName =
  "min-h-28 w-full resize-y rounded-lg border border-input bg-muted px-3 py-2 text-sm text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"

function DetailHeader({
  backHref,
  backLabel,
  title,
  eyebrow,
  body,
  actions,
  children,
}: {
  backHref: string
  backLabel: string
  title: string
  eyebrow: string
  body: string
  actions?: ReactNode
  children?: ReactNode
}) {
  return (
    <ProjectPageHeader
      leading={
        <Link
          href={backHref}
          className="flex shrink-0 items-center gap-0.5 rounded-lg py-1 pe-1.5 text-[13px] font-medium text-muted-foreground outline-none hover:bg-accent hover:text-accent-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          <ArrowLeft size={16} strokeWidth={1.8} aria-hidden="true" />
          {backLabel}
        </Link>
      }
      eyebrow={labelize(eyebrow)}
      title={title}
      subtitle={body || undefined}
      actions={actions}
    >
      {children}
    </ProjectPageHeader>
  )
}

/** Nested record inside a panel (capability, case, component, pattern, question). */
function RecordItem({ title, meta, body }: { title: ReactNode; meta?: ReactNode; body?: ReactNode }) {
  return (
    <div className={projectInsetClassName}>
      <p className="break-words text-body2 font-medium text-foreground">{title}</p>
      {meta ? <p className="text-label text-muted-foreground">{meta}</p> : null}
      {body ? <p className="mt-0.5 break-words text-label text-muted-foreground">{body}</p> : null}
    </div>
  )
}

function RecordStack({ empty, children }: { empty: string; children: ReactNode[] }) {
  return children.length ? <div className="space-y-2">{children}</div> : <p className="text-body2 text-muted-foreground">{empty}</p>
}

/** Label / value rows separated by hairlines. */
function FactRow({ label, value }: { label: ReactNode; value: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-border py-2 text-body2 last:border-0">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className="min-w-0 break-words text-right font-medium text-foreground">{value}</span>
    </div>
  )
}

function LinkPanel({ project }: { project: ProjectsApiProjectDetail["project"] }) {
  const url = project.github_url ?? project.website_url ?? project.canonical_url
  return (
    <ProjectPanel title="Links">
      {url ? (
        <Button asChild size="sm">
          <a href={url} target="_blank" rel="noreferrer">
            Open Source
            <ExternalLink strokeWidth={1.8} />
          </a>
        </Button>
      ) : (
        <p className="text-body2 text-muted-foreground">No public URL in the current artifact.</p>
      )}
    </ProjectPanel>
  )
}

function Panel({ title, body }: { title: string; body: string }) {
  return (
    <ProjectPanel title={title}>
      <p className="break-words text-body2 text-muted-foreground">{body || "Unavailable"}</p>
    </ProjectPanel>
  )
}

function JsonPanel({ title, value }: { title: string; value: unknown }) {
  return (
    <ProjectPanel title={title}>
      <ProjectCodeBlock>{JSON.stringify(value, null, 2)}</ProjectCodeBlock>
    </ProjectPanel>
  )
}

function CapabilitiesPanel({ capabilities }: { capabilities: unknown[] }) {
  const items = Array.isArray(capabilities) ? capabilities : []
  return (
    <ProjectPanel title="Capabilities">
      <RecordStack empty="No capabilities available">
        {items.map((cap, idx) => {
          const record = asRecord(cap)
          return <RecordItem key={idx} title={textValue(record?.name) || textValue(cap)} body={textValue(record?.description) || undefined} />
        })}
      </RecordStack>
    </ProjectPanel>
  )
}

function CasesPanel({ cases }: { cases: unknown[] }) {
  const items = Array.isArray(cases) ? cases : []
  return (
    <ProjectPanel title="Cases">
      <RecordStack empty="No cases available">
        {items.map((item, idx) => {
          const record = asRecord(item)
          return <RecordItem key={idx} title={textValue(record?.title) || textValue(item)} meta={textValue(record?.module_type) || undefined} />
        })}
      </RecordStack>
    </ProjectPanel>
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
    <ProjectPanel title="Metrics">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {fields.map(([label, value]) => (
          <ProjectMetric key={label} label={label} value={value} />
        ))}
      </div>
    </ProjectPanel>
  )
}

function WatchPanel({ status }: { status?: unknown }) {
  const watchStatus = textValue(asRecord(status)?.status) || "not_watched"
  return (
    <ProjectPanel title="Watch Status">
      <p className="text-body2 text-muted-foreground">{watchStatus}</p>
    </ProjectPanel>
  )
}

function ActionsPanel({ actions }: { actions?: unknown }) {
  const items = Array.isArray(actions) ? actions : []
  if (!items.length) return null
  return (
    <ProjectPanel title="Recommended">
      <ul className="list-disc space-y-1 ps-4 text-body2 text-muted-foreground">
        {items.map((action, idx) => (
          <li key={idx} className="break-words">
            {textValue(asRecord(action)?.title) || textValue(action)}
          </li>
        ))}
      </ul>
    </ProjectPanel>
  )
}

function IntegrationProfilePanel({ profile }: { profile: ProjectsApiTool["profile"] }) {
  return (
    <ProjectPanel title="Integration Profile">
      <FactRow label="Type" value={profile.tool_type} />
      <FactRow label="Difficulty" value={profile.integration_difficulty} />
      <FactRow label="Has API" value={profile.has_api ? "Yes" : "No"} />
      <FactRow label="Has CLI" value={profile.has_cli ? "Yes" : "No"} />
      <FactRow label="Python SDK" value={profile.has_python_sdk ? "Yes" : "No"} />
      <FactRow label="Docker" value={profile.has_docker ? "Yes" : "No"} />
    </ProjectPanel>
  )
}

function ComponentsPanel({ components }: { components?: unknown }) {
  const items = Array.isArray(components) ? components : []
  return (
    <ProjectPanel title="Components">
      <RecordStack empty="No components available">
        {items.map((comp, idx) => {
          const record = asRecord(comp)
          return (
            <RecordItem
              key={idx}
              title={textValue(record?.name) || "Unnamed component"}
              meta={record?.component_type ? textValue(record.component_type) : undefined}
              body={record?.responsibility ? textValue(record.responsibility) : undefined}
            />
          )
        })}
      </RecordStack>
    </ProjectPanel>
  )
}

function PatternsPanel({ patterns }: { patterns?: unknown }) {
  const items = Array.isArray(patterns) ? patterns : []
  return (
    <ProjectPanel title="Patterns">
      <RecordStack empty="No patterns available">
        {items.map((pat, idx) => {
          const record = asRecord(pat)
          return (
            <RecordItem
              key={idx}
              title={textValue(record?.name) || "Unnamed pattern"}
              meta={record?.pattern_type ? textValue(record.pattern_type) : undefined}
              body={record?.explanation ? textValue(record.explanation) : undefined}
            />
          )
        })}
      </RecordStack>
    </ProjectPanel>
  )
}
function SectionsPanel({ sections }: { sections?: unknown }) {
  const items = Array.isArray(sections) ? sections : []
  return (
    <ProjectPanel title="Sections">
      <RecordStack empty="No sections available">
        {items.map((sec, idx) => {
          const record = asRecord(sec)
          const sectionItems = Array.isArray(record?.items) ? record.items : []
          return (
            <RecordItem
              key={idx}
              title={textValue(record?.title) || "Untitled section"}
              body={
                sectionItems.length ? (
                  <span className="mt-1 block space-y-0.5">
                    {sectionItems.map((item, i) => (
                      <span key={i} className="block">
                        • {textValue(asRecord(item)?.title) || textValue(item)}
                      </span>
                    ))}
                  </span>
                ) : undefined
              }
            />
          )
        })}
      </RecordStack>
    </ProjectPanel>
  )
}

function RequirementProfilePanel({ profile }: { profile?: unknown }) {
  const record = asRecord(profile)
  if (!record) return null
  return (
    <ProjectPanel title="Requirement Profile">
      {Object.entries(record).map(([key, value]) => (
        <FactRow key={key} label={key} value={String(value)} />
      ))}
    </ProjectPanel>
  )
}

function QuestionsPanel({ questions }: { questions?: unknown }) {
  const items = Array.isArray(questions) ? questions : []
  return (
    <ProjectPanel title="Questions">
      <RecordStack empty="No questions available">
        {items.map((question, idx) => {
          const record = asRecord(question)
          const answeredValue = record?.answered_value
          return (
            <RecordItem
              key={idx}
              title={`Q${idx + 1}`}
              meta={textValue(record?.question)}
              body={answeredValue ? `Answer: ${String(answeredValue)}` : undefined}
            />
          )
        })}
      </RecordStack>
    </ProjectPanel>
  )
}

function SolutionPanel({ solution }: { solution?: unknown }) {
  if (!solution) return null
  const text = typeof solution === "string" ? solution : JSON.stringify(solution, null, 2)
  return (
    <ProjectPanel title="Solution">
      <ProjectCodeBlock className="whitespace-pre-wrap break-words">{text}</ProjectCodeBlock>
    </ProjectPanel>
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
