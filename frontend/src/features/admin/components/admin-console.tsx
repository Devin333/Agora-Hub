"use client"

import { useEffect, useMemo, useState } from "react"
import { AlertTriangle, CheckCircle2, Clock3, FileText, ShieldAlert } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { AdminHeader } from "@/features/admin/components/admin-header"
import { AdminSidebar } from "@/features/admin/components/admin-sidebar"
import { StatusPill } from "@/features/admin/components/status-pill"
import {
  agents,
  attentionItems,
  drafts,
  metrics,
  pipelineNodes,
  publishingChannels,
  qualityGates,
  reviewTasks,
  sources,
  topicClusters
} from "@/features/admin/lib/mock-data"
import { pageTitleKey, pick, ui } from "@/features/admin/lib/i18n"
import type { AdminLang, AdminPage, PipelineNode, ReviewTask, SourceRecord } from "@/features/admin/types"
import { cn } from "@/lib/utils"
import { useUiStore } from "@/stores/ui-store"

export function AdminConsole() {
  const [activePage, setActivePage] = useState<AdminPage>("overview")
  const lang = useUiStore((state) => state.locale) as AdminLang
  const [selectedReviewId, setSelectedReviewId] = useState(reviewTasks[0]?.id ?? "")
  const [selectedPipelineId, setSelectedPipelineId] = useState(pipelineNodes[0]?.id ?? "")
  const [selectedSourceId, setSelectedSourceId] = useState(sources[0]?.id ?? "")
  const [selectedDraftId, setSelectedDraftId] = useState(drafts[0]?.id ?? "")
  const [selectedAgentId, setSelectedAgentId] = useState(agents[0]?.id ?? "")

  const copy = ui[lang]
  const selectedReview = useMemo(
    () => reviewTasks.find((task) => task.id === selectedReviewId) ?? reviewTasks[0],
    [selectedReviewId]
  )
  const selectedPipeline = useMemo(
    () => pipelineNodes.find((node) => node.id === selectedPipelineId) ?? pipelineNodes[0],
    [selectedPipelineId]
  )
  const selectedSource = useMemo(
    () => sources.find((source) => source.id === selectedSourceId) ?? sources[0],
    [selectedSourceId]
  )
  const selectedDraft = useMemo(() => drafts.find((draft) => draft.id === selectedDraftId) ?? drafts[0], [selectedDraftId])
  const selectedAgent = useMemo(() => agents.find((agent) => agent.id === selectedAgentId) ?? agents[0], [selectedAgentId])

  useEffect(() => {
    document.title = `${copy[pageTitleKey[activePage]]} - ${copy.intelligenceConsole}`
  }, [activePage, copy])

  function openReview(reviewId: string) {
    setSelectedReviewId(reviewId)
    setActivePage("review")
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <AdminSidebar activePage={activePage} lang={lang} onPageChange={setActivePage} />
      <div className="min-w-0 lg:pl-72">
        <AdminHeader activePage={activePage} lang={lang} />
        <main className="mx-auto max-w-[1500px] space-y-6 px-4 py-5 lg:px-6">
          {activePage === "overview"
            ? renderOverview({ lang, selectedReview, selectedPipeline, onOpenReview: openReview, onSelectPipeline: setSelectedPipelineId })
            : null}
          {activePage === "ingestion"
            ? renderSourcesPage({ lang, selectedSource, onSelectSource: setSelectedSourceId, registry: false })
            : null}
          {activePage === "pipeline"
            ? renderPipelinePage({ lang, selectedPipeline, onSelectPipeline: setSelectedPipelineId })
            : null}
          {activePage === "review"
            ? renderReviewPage({ lang, selectedReview, onSelectReview: setSelectedReviewId })
            : null}
          {activePage === "content"
            ? renderContentStudio({ lang, selectedDraftId, selectedDraft, onSelectDraft: setSelectedDraftId })
            : null}
          {activePage === "topics" ? renderTopicClusters({ lang }) : null}
          {activePage === "sources"
            ? renderSourcesPage({ lang, selectedSource, onSelectSource: setSelectedSourceId, registry: true })
            : null}
          {activePage === "agents"
            ? renderAgentRuntime({ lang, selectedAgentId, selectedAgent, onSelectAgent: setSelectedAgentId })
            : null}
          {activePage === "gates" ? renderQualityGates({ lang }) : null}
          {activePage === "publishing" ? renderPublishing({ lang }) : null}
          {activePage === "settings" ? renderSettings({ lang }) : null}
        </main>
      </div>
    </div>
  )
}

function renderOverview({
  lang,
  selectedReview,
  selectedPipeline,
  onOpenReview,
  onSelectPipeline
}: {
  lang: AdminLang
  selectedReview: ReviewTask
  selectedPipeline: PipelineNode
  onOpenReview: (reviewId: string) => void
  onSelectPipeline: (nodeId: string) => void
}) {
  const copy = ui[lang]

  return (
    <div className="space-y-6">
      <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-4" aria-label={copy.metrics}>
        {metrics.map((metric) => (
          <MetricCard key={metric.id} metric={metric} lang={lang} />
        ))}
      </section>

      <section className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_420px]">
        <div className="space-y-6">
          <Panel title={copy.needsAttention} description={lang === "zh" ? "系统路由到人工校验的高优先级事项。" : "High-priority items routed to human review."}>
            <div className="grid gap-3">
              {attentionItems.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className="rounded-md border border-border bg-card p-4 text-left transition-colors hover:bg-secondary/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  onClick={() => onOpenReview(item.reviewId)}
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="font-medium text-foreground">{pick(item.title, lang)}</p>
                      <p className="mt-1 text-sm text-muted-foreground">{pick(item.reason, lang)}</p>
                    </div>
                    <span className="rounded-md border border-warning/30 bg-warning/10 px-2 py-1 text-xs font-medium text-warning">
                      {pick(item.priority, lang)}
                    </span>
                  </div>
                  <p className="mt-3 text-xs text-muted-foreground">{pick(item.type, lang)}</p>
                </button>
              ))}
            </div>
          </Panel>

          <Panel title={copy.pipelineHealth} description={lang === "zh" ? "点击节点查看当前流水线输出证据。" : "Click a node to inspect pipeline evidence."}>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {pipelineNodes.map((node) => (
                <PipelineNodeButton
                  key={node.id}
                  node={node}
                  lang={lang}
                  selected={selectedPipeline.id === node.id}
                  onClick={() => onSelectPipeline(node.id)}
                />
              ))}
            </div>
          </Panel>
        </div>

        <div className="space-y-6">
          <Panel title={copy.reviewPreview} description={lang === "zh" ? "最需要编辑介入的任务。" : "Tasks most in need of editorial intervention."}>
            <div className="divide-y divide-border rounded-md border border-border">
              {reviewTasks.map((task) => (
                <button
                  key={task.id}
                  type="button"
                  className={cn(
                    "block w-full p-3 text-left text-sm transition-colors hover:bg-secondary/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    selectedReview.id === task.id && "bg-secondary"
                  )}
                  onClick={() => onOpenReview(task.id)}
                >
                  <p className="font-medium text-foreground">{pick(task.title, lang)}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {pick(task.gate, lang)} · {task.confidence}%
                  </p>
                </button>
              ))}
            </div>
          </Panel>

          <ReviewDetailPanel task={selectedReview} lang={lang} />
          <PipelineInspector node={selectedPipeline} lang={lang} compact />
        </div>
      </section>
    </div>
  )
}

function renderSourcesPage({
  lang,
  selectedSource,
  onSelectSource,
  registry
}: {
  lang: AdminLang
  selectedSource: SourceRecord
  onSelectSource: (sourceId: string) => void
  registry: boolean
}) {
  const copy = ui[lang]

  return (
    <section className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_390px]">
      <Panel
        title={registry ? copy.sources : copy.ingestion}
        description={
          registry
            ? lang === "zh"
              ? "管理信源配置、可靠性与默认采集策略。"
              : "Manage source configuration, reliability, and default collection policy."
            : lang === "zh"
              ? "监控爬虫状态、信源新鲜度和失败来源。"
              : "Monitor crawler status, source freshness, and failed sources."
        }
      >
        <div className="overflow-x-auto rounded-md border border-border">
          <div className="min-w-[820px]">
            <div className="grid grid-cols-[1.35fr_1fr_0.8fr_1fr_0.7fr_0.8fr] gap-3 bg-secondary/70 px-4 py-3 text-xs font-medium uppercase text-muted-foreground">
              <span>{copy.sourceName}</span>
              <span>{copy.sourceType}</span>
              <span>{copy.status}</span>
              <span>{copy.lastFetch}</span>
              <span>{copy.itemCount}</span>
              <span>{copy.reliability}</span>
            </div>
            <div className="divide-y divide-border">
              {sources.map((source) => (
                <button
                  key={source.id}
                  type="button"
                  className={cn(
                    "grid w-full grid-cols-[1.35fr_1fr_0.8fr_1fr_0.7fr_0.8fr] gap-3 px-4 py-3 text-left text-sm transition-colors hover:bg-secondary/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    selectedSource.id === source.id && "bg-secondary"
                  )}
                  onClick={() => onSelectSource(source.id)}
                >
                  <span>
                    <span className="block font-medium text-foreground">{source.name}</span>
                    <span className="block text-xs text-muted-foreground">{source.id}</span>
                  </span>
                  <span className="text-muted-foreground">{pick(source.type, lang)}</span>
                  <StatusPill status={source.status} lang={lang} />
                  <span className="text-muted-foreground">{pick(source.lastFetch, lang)}</span>
                  <span className="text-muted-foreground">{source.itemCount}</span>
                  <span className="font-medium text-foreground">{source.reliability}%</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      </Panel>

      <SourceDetailPanel source={selectedSource} lang={lang} />
    </section>
  )
}

function renderPipelinePage({
  lang,
  selectedPipeline,
  onSelectPipeline
}: {
  lang: AdminLang
  selectedPipeline: PipelineNode
  onSelectPipeline: (nodeId: string) => void
}) {
  const copy = ui[lang]

  return (
    <section className="space-y-6">
      <div className="rounded-md border border-border bg-card p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="font-mono text-sm text-muted-foreground">{copy.runId}</p>
            <h2 className="mt-1 text-lg font-semibold text-foreground">
              {copy.scheduled} · {copy.pipelineDuration}
            </h2>
          </div>
          <StatusPill status="running" lang={lang} />
        </div>
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_420px]">
        <Panel title={copy.pipeline} description={lang === "zh" ? "点击节点查看输入、输出与重跑操作。" : "Click a node to inspect input, output, and rerun actions."}>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {pipelineNodes.map((node) => (
              <PipelineNodeButton
                key={node.id}
                node={node}
                lang={lang}
                selected={selectedPipeline.id === node.id}
                onClick={() => onSelectPipeline(node.id)}
              />
            ))}
          </div>
        </Panel>
        <PipelineInspector node={selectedPipeline} lang={lang} />
      </div>
    </section>
  )
}

function renderReviewPage({
  lang,
  selectedReview,
  onSelectReview
}: {
  lang: AdminLang
  selectedReview: ReviewTask
  onSelectReview: (reviewId: string) => void
}) {
  const copy = ui[lang]

  return (
    <section className="grid gap-6 xl:grid-cols-[430px_minmax(0,1fr)]">
      <Panel title={copy.review} description={lang === "zh" ? "只处理不确定、高风险、证据不足或发布敏感的内容。" : "Only uncertain, risky, incomplete, or publish-sensitive items are routed here."}>
        <div className="space-y-3">
          {reviewTasks.map((task) => (
            <button
              key={task.id}
              type="button"
              className={cn(
                "w-full rounded-md border border-border bg-card p-4 text-left transition-colors hover:bg-secondary/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                selectedReview.id === task.id && "bg-secondary"
              )}
              onClick={() => onSelectReview(task.id)}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="font-medium text-foreground">{pick(task.title, lang)}</p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {pick(task.taskType, lang)} · {task.source}
                  </p>
                </div>
                <span className="rounded-md border border-border bg-card px-2 py-1 text-xs font-medium text-muted-foreground">
                  {task.confidence}%
                </span>
              </div>
              <p className="mt-3 text-xs text-muted-foreground">
                {copy.gate}: {pick(task.gate, lang)} · {copy.risk}: {pick(task.risk, lang)}
              </p>
            </button>
          ))}
        </div>
      </Panel>

      <ReviewDetailPanel task={selectedReview} lang={lang} wide />
    </section>
  )
}

function renderContentStudio({
  lang,
  selectedDraftId,
  selectedDraft,
  onSelectDraft
}: {
  lang: AdminLang
  selectedDraftId: string
  selectedDraft: (typeof drafts)[number]
  onSelectDraft: (draftId: string) => void
}) {
  const copy = ui[lang]

  return (
    <section className="grid gap-6 xl:grid-cols-[360px_minmax(0,1fr)]">
      <Panel title={copy.content} description={lang === "zh" ? "编辑生成草稿并请求审核。" : "Edit generated drafts and request review."}>
        <div className="space-y-3">
          {drafts.map((draft) => (
            <button
              key={draft.id}
              type="button"
              className={cn(
                "w-full rounded-md border border-border bg-card p-4 text-left transition-colors hover:bg-secondary/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                selectedDraftId === draft.id && "bg-secondary"
              )}
              onClick={() => onSelectDraft(draft.id)}
            >
              <p className="font-medium text-foreground">{pick(draft.title, lang)}</p>
              <p className="mt-1 text-sm text-muted-foreground">{pick(draft.status, lang)}</p>
            </button>
          ))}
        </div>
      </Panel>

      <Panel title={lang === "zh" ? "编辑区" : "Editor Workspace"} description={lang === "zh" ? "v0.1 使用本地草稿数据，不保存到后端。" : "v0.1 uses local draft data and does not save to a backend."}>
        <div className="space-y-4">
          <Field label={copy.title}>
            <Input value={pick(selectedDraft.title, lang)} readOnly />
          </Field>
          <Field label={copy.body}>
            <textarea
              className="min-h-64 w-full rounded-md border border-input bg-card px-3 py-2 text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              value={pick(selectedDraft.body, lang)}
              readOnly
            />
          </Field>
          <Field label={copy.draftStatus}>
            <p className="rounded-md border border-border bg-secondary/60 px-3 py-2 text-sm text-muted-foreground">
              {pick(selectedDraft.status, lang)}
            </p>
          </Field>
          <div className="flex flex-wrap gap-2">
            <Button type="button">{copy.saveDraft}</Button>
            <Button type="button" variant="outline">
              {copy.requestReview}
            </Button>
          </div>
        </div>
      </Panel>
    </section>
  )
}

function renderTopicClusters({ lang }: { lang: AdminLang }) {
  const copy = ui[lang]

  return (
    <Panel title={copy.topics} description={lang === "zh" ? "管理自动聚类主题的合并、拆分与热点标记。" : "Manage auto-clustered topic merge, split, and hot labels."}>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {topicClusters.map((topic) => (
          <Card key={topic.id}>
            <CardHeader>
              <CardTitle>{pick(topic.name, lang)}</CardTitle>
              <p className="text-sm text-muted-foreground">
                {topic.itemCount} {copy.itemCount} · {pick(topic.velocity, lang)}
              </p>
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-muted-foreground">{pick(topic.suggestedAction, lang)}</p>
              <div className="flex flex-wrap gap-2">
                {topic.tags.map((tag) => (
                  <span key={pick(tag, lang)} className="rounded-md border border-border bg-secondary px-2 py-1 text-xs text-muted-foreground">
                    {pick(tag, lang)}
                  </span>
                ))}
              </div>
              <div className="flex flex-wrap gap-2">
                <Button type="button" size="sm" variant="outline">
                  {copy.merge}
                </Button>
                <Button type="button" size="sm" variant="outline">
                  {copy.split}
                </Button>
                <Button type="button" size="sm">
                  {copy.markHot}
                </Button>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </Panel>
  )
}

function renderAgentRuntime({
  lang,
  selectedAgentId,
  selectedAgent,
  onSelectAgent
}: {
  lang: AdminLang
  selectedAgentId: string
  selectedAgent: (typeof agents)[number]
  onSelectAgent: (agentId: string) => void
}) {
  const copy = ui[lang]

  return (
    <section className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_420px]">
      <Panel title={copy.agents} description={lang === "zh" ? "观察 Agent 执行、工具、成本与健康状态。" : "Observe agent execution, tools, cost, and health."}>
        <div className="grid gap-3 md:grid-cols-3">
          {agents.map((agent) => (
            <button
              key={agent.id}
              type="button"
              className={cn(
                "rounded-md border border-border bg-card p-4 text-left transition-colors hover:bg-secondary/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                selectedAgentId === agent.id && "bg-secondary"
              )}
              onClick={() => onSelectAgent(agent.id)}
            >
              <div className="flex items-start justify-between gap-2">
                <p className="font-medium text-foreground">{agent.name}</p>
                <StatusPill status={agent.status} lang={lang} />
              </div>
              <div className="mt-4 grid gap-2 text-sm">
                <SummaryLine label={copy.runCount} value={String(agent.runCount)} />
                <SummaryLine label={copy.successRate} value={agent.successRate} />
                <SummaryLine label={copy.cost} value={agent.cost} />
              </div>
              <p className="mt-3 text-xs text-muted-foreground">{agent.tools.join(", ")}</p>
            </button>
          ))}
        </div>
      </Panel>

      <Panel title={copy.traceViewer} description={selectedAgent.name}>
        <div className="mb-4 flex flex-wrap gap-2">
          {selectedAgent.tools.map((tool) => (
            <span key={tool} className="rounded-md border border-border bg-secondary px-2 py-1 text-xs text-muted-foreground">
              {tool}
            </span>
          ))}
        </div>
        <ol className="space-y-3">
          {selectedAgent.trace.map((step, index) => (
            <li key={pick(step, lang)} className="flex gap-3 rounded-md border border-border bg-card p-3 text-sm">
              <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-secondary text-xs font-medium text-muted-foreground">
                {index + 1}
              </span>
              <span className="text-muted-foreground">{pick(step, lang)}</span>
            </li>
          ))}
        </ol>
      </Panel>
    </section>
  )
}

function renderQualityGates({ lang }: { lang: AdminLang }) {
  const copy = ui[lang]

  return (
    <Panel title={copy.gates} description={lang === "zh" ? "查看规则通过、警告与失败数量。" : "Inspect rule pass, warning, and failure counts."}>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {qualityGates.map((gate) => (
          <Card key={gate.id}>
            <CardHeader className="flex-row items-start justify-between gap-3">
              <div>
                <CardTitle>{pick(gate.name, lang)}</CardTitle>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">{pick(gate.rule, lang)}</p>
              </div>
              <StatusPill status={gate.status} lang={lang} />
            </CardHeader>
            <CardContent className="grid grid-cols-3 gap-2 text-center text-sm">
              <Counter label={copy.passed} value={gate.passed} tone="success" />
              <Counter label={copy.warning} value={gate.warning} tone="warning" />
              <Counter label={copy.failed} value={gate.failed} tone="danger" />
            </CardContent>
          </Card>
        ))}
      </div>
    </Panel>
  )
}

function renderPublishing({ lang }: { lang: AdminLang }) {
  const copy = ui[lang]

  return (
    <section className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-foreground">{copy.publishing}</h2>
          <p className="text-sm text-muted-foreground">
            {lang === "zh" ? "控制哪些已审核内容进入公开产品。" : "Control what approved content enters the public product."}
          </p>
        </div>
        <Button type="button">{copy.publishBatch}</Button>
      </div>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {publishingChannels.map((channel) => (
          <Card key={channel.id}>
            <CardHeader>
              <CardTitle>{channel.name}</CardTitle>
              <p className="text-sm text-muted-foreground">{pick(channel.description, lang)}</p>
            </CardHeader>
            <CardContent className="space-y-4">
              <div>
                <p className="text-3xl font-semibold text-foreground">{channel.readyCount}</p>
                <p className="text-sm text-muted-foreground">{copy.ready}</p>
              </div>
              <Button type="button" variant="outline" size="sm">
                {copy.preview}
              </Button>
            </CardContent>
          </Card>
        ))}
      </div>
    </section>
  )
}

function renderSettings({ lang }: { lang: AdminLang }) {
  const copy = ui[lang]

  return (
    <Panel title={copy.settings} description={copy.settingsPlaceholder}>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {[
          { icon: ShieldAlert, label: lang === "zh" ? "角色权限" : "Role Permissions" },
          { icon: FileText, label: lang === "zh" ? "发布规则" : "Publishing Rules" },
          { icon: CheckCircle2, label: lang === "zh" ? "质量阈值" : "Quality Thresholds" },
          { icon: Clock3, label: lang === "zh" ? "运行限制" : "Runtime Limits" }
        ].map((item) => (
          <div key={item.label} className="rounded-md border border-border bg-card p-4">
            <item.icon className="mb-3 size-5 text-muted-foreground" />
            <p className="font-medium text-foreground">{item.label}</p>
            <p className="mt-1 text-sm text-muted-foreground">{lang === "zh" ? "占位配置项" : "Placeholder setting"}</p>
          </div>
        ))}
      </div>
    </Panel>
  )
}

function MetricCard({ metric, lang }: { metric: (typeof metrics)[number]; lang: AdminLang }) {
  const icons = {
    ok: CheckCircle2,
    warning: AlertTriangle,
    failed: ShieldAlert,
    review: AlertTriangle,
    running: Clock3,
    blocked: ShieldAlert
  }
  const Icon = icons[metric.status]

  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-sm text-muted-foreground">{pick(metric.label, lang)}</p>
            <p className="mt-2 text-3xl font-semibold text-foreground">{metric.value}</p>
          </div>
          <Icon className="size-5 text-muted-foreground" />
        </div>
        <div className="mt-3 flex items-center justify-between gap-2">
          <p className="text-sm text-muted-foreground">{pick(metric.delta, lang)}</p>
          <StatusPill status={metric.status} lang={lang} />
        </div>
      </CardContent>
    </Card>
  )
}

function PipelineNodeButton({
  node,
  lang,
  selected,
  onClick
}: {
  node: PipelineNode
  lang: AdminLang
  selected: boolean
  onClick: () => void
}) {
  const copy = ui[lang]

  return (
    <button
      type="button"
      className={cn(
        "rounded-md border border-border bg-card p-4 text-left transition-colors hover:bg-secondary/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        selected && "bg-secondary"
      )}
      onClick={onClick}
    >
      <div className="flex items-start justify-between gap-3">
        <p className="font-medium text-foreground">{node.name}</p>
        <StatusPill status={node.status} lang={lang} />
      </div>
      <div className="mt-3 grid gap-1 text-sm text-muted-foreground">
        <p>
          {copy.processed}: {node.processed.toLocaleString()}
        </p>
        <p>
          {copy.duration}: {node.duration}
        </p>
      </div>
      <p className="mt-3 text-sm text-muted-foreground">{pick(node.detail, lang)}</p>
    </button>
  )
}

function PipelineInspector({ node, lang, compact = false }: { node: PipelineNode; lang: AdminLang; compact?: boolean }) {
  const copy = ui[lang]

  return (
    <Panel title={compact ? node.name : `${node.name} ${lang === "zh" ? "检查器" : "Inspector"}`} description={pick(node.detail, lang)}>
      <div className={cn("grid gap-3", compact ? "text-sm" : "sm:grid-cols-2")}>
        <SummaryBox label={copy.status} value={<StatusPill status={node.status} lang={lang} />} />
        <SummaryBox label={copy.processed} value={node.processed.toLocaleString()} />
        <SummaryBox label={copy.duration} value={node.duration} />
        <SummaryBox label={copy.artifactPath} value={node.artifactPath} />
      </div>
      <div className="mt-4 rounded-md border border-border bg-secondary/40 p-3 text-sm text-muted-foreground">
        <p className="font-medium text-foreground">{copy.outputDetail}</p>
        <p className="mt-1">{pick(node.output, lang)}</p>
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        <Button type="button" size="sm" variant="outline">
          {copy.viewInput}
        </Button>
        <Button type="button" size="sm" variant="outline">
          {copy.viewOutput}
        </Button>
        <Button type="button" size="sm">
          {copy.rerunNode}
        </Button>
      </div>
    </Panel>
  )
}

function ReviewDetailPanel({ task, lang, wide = false }: { task: ReviewTask; lang: AdminLang; wide?: boolean }) {
  const copy = ui[lang]

  return (
    <Panel title={copy.reviewDetail} description={pick(task.title, lang)}>
      <div className="mb-4 grid gap-3 sm:grid-cols-3">
        <SummaryBox label={copy.gate} value={pick(task.gate, lang)} />
        <SummaryBox label={copy.risk} value={pick(task.risk, lang)} />
        <SummaryBox label={copy.confidence} value={`${task.confidence}%`} />
      </div>
      <div className={cn("grid gap-4", wide ? "xl:grid-cols-3" : "md:grid-cols-1")}>
        <DetailBlock title={copy.rawInput}>{pick(task.rawInput, lang)}</DetailBlock>
        <DetailBlock title={copy.aiOutput}>{pick(task.aiOutput, lang)}</DetailBlock>
        <DetailBlock title={copy.evidence}>
          <ul className="space-y-2">
            {task.evidence.map((item) => (
              <li key={pick(item, lang)} className="rounded-md border border-border bg-card px-3 py-2">
                {pick(item, lang)}
              </li>
            ))}
          </ul>
        </DetailBlock>
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        <Button type="button">{copy.approve}</Button>
        <Button type="button" variant="outline">
          {copy.edit}
        </Button>
        <Button type="button" variant="outline">
          {copy.sendVerifier}
        </Button>
        <Button type="button" variant="destructive">
          {copy.reject}
        </Button>
      </div>
    </Panel>
  )
}

function SourceDetailPanel({ source, lang }: { source: SourceRecord; lang: AdminLang }) {
  const copy = ui[lang]

  return (
    <Panel title={source.name} description={source.id}>
      <div className="grid gap-3">
        <SummaryBox label={copy.reliability} value={`${source.reliability}%`} />
        <SummaryBox label={copy.itemCount} value={source.itemCount.toLocaleString()} />
        <SummaryBox label={copy.frequency} value={pick(source.frequency, lang)} />
        <SummaryBox label={copy.retryPolicy} value={pick(source.retryPolicy, lang)} />
      </div>
      <div className="mt-4">
        <p className="mb-2 text-sm font-medium text-foreground">{lang === "zh" ? "最新原始数据" : "Latest Raw Items"}</p>
        <div className="space-y-2">
          {source.latestItems.map((item) => (
            <p key={pick(item, lang)} className="rounded-md border border-border bg-card px-3 py-2 text-sm text-muted-foreground">
              {pick(item, lang)}
            </p>
          ))}
        </div>
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        <Button type="button" size="sm">
          {copy.runSource}
        </Button>
        <Button type="button" size="sm" variant="outline">
          {copy.editSource}
        </Button>
        <Button type="button" size="sm" variant="outline">
          {copy.openRaw}
        </Button>
      </div>
    </Panel>
  )
}

function Panel({
  title,
  description,
  children
}: {
  title: string
  description?: string
  children: React.ReactNode
}) {
  return (
    <section className="rounded-md border border-border bg-card shadow-sm">
      <div className="border-b border-border px-4 py-3">
        <h2 className="text-base font-semibold text-foreground">{title}</h2>
        {description ? <p className="mt-1 text-sm leading-6 text-muted-foreground">{description}</p> : null}
      </div>
      <div className="p-4">{children}</div>
    </section>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-2 block text-sm font-medium text-foreground">{label}</span>
      {children}
    </label>
  )
}

function DetailBlock({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-border bg-secondary/40 p-3">
      <p className="mb-2 text-sm font-medium text-foreground">{title}</p>
      <div className="text-sm leading-6 text-muted-foreground">{children}</div>
    </div>
  )
}

function SummaryBox({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-secondary/40 p-3">
      <p className="text-xs font-medium uppercase text-muted-foreground">{label}</p>
      <div className="mt-1 break-words text-sm font-medium text-foreground">{value}</div>
    </div>
  )
}

function SummaryLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium text-foreground">{value}</span>
    </div>
  )
}

function Counter({ label, value, tone }: { label: string; value: number; tone: "success" | "warning" | "danger" }) {
  const toneClass = tone === "success" ? "text-success" : tone === "warning" ? "text-warning" : "text-destructive"

  return (
    <div className="rounded-md border border-border bg-secondary/40 p-3">
      <p className={cn("text-xl font-semibold", toneClass)}>{value}</p>
      <p className="mt-1 text-xs text-muted-foreground">{label}</p>
    </div>
  )
}
