"use client"

import Link from "next/link"
import { useRouter } from "next/navigation"
import { useMemo, useState } from "react"
import {
  ArrowRight,
  BookOpen,
  Check,
  FileText,
  GitBranch,
  MessageSquareText,
  Network,
  Newspaper,
  Search,
} from "lucide-react"
import { cn } from "@/lib/utils"
import type {
  EvidenceEdge,
  EvidenceGraphPeriod,
  EvidenceGraphResponse,
  EvidenceGraphTimelineItem,
  EvidenceNode,
  EvidenceNodeType,
} from "@/types/evidence-graph"

const evidenceTypes: Array<{ type: EvidenceNodeType; label: string }> = [
  { type: "paper", label: "Papers" },
  { type: "project", label: "Projects" },
  { type: "news", label: "News" },
  { type: "community_signal", label: "Community" },
]

const periods = [
  { value: "all", label: "全部" },
  { value: "monthly", label: "本月" },
  { value: "weekly", label: "本周" },
  { value: "daily", label: "今日" },
]

const nodeTypeLabels: Record<EvidenceNodeType, string> = {
  topic: "Topic",
  paper: "Paper",
  project: "Project",
  news: "News",
  community_signal: "Community",
  company: "Company",
  model: "Model",
  method: "Method",
  task: "Task",
  report: "Report",
}

const edgeLabels: Record<string, string> = {
  mentions: "提及",
  implements: "实现",
  cites: "引用",
  discusses: "讨论",
  supports: "支撑",
  contradicts: "反证",
  derived_from: "来源",
  same_topic: "同主题",
  released_by: "发布方",
  reported_by: "报告收录",
}

export function EvidenceGraphExplorer({ data }: { data: EvidenceGraphResponse }) {
  const router = useRouter()
  const [topic, setTopic] = useState(data.query.topic ?? "")
  const [entity, setEntity] = useState(data.query.entity ?? "")
  const [period, setPeriod] = useState(data.query.period ?? "all")
  const [nodeTypes, setNodeTypes] = useState<EvidenceNodeType[]>(data.query.nodeTypes ?? [])
  const initialNodeId = data.summary.keyEvidenceNodeIds[0] ?? data.nodes.find((node) => node.type !== "topic")?.id ?? data.nodes[0]?.id
  const [selectedNodeId, setSelectedNodeId] = useState(initialNodeId)
  const nodeById = useMemo(() => new Map(data.nodes.map((node) => [node.id, node])), [data.nodes])
  const selectedNode = nodeById.get(selectedNodeId ?? "") ?? data.nodes.find((node) => node.type !== "topic") ?? data.nodes[0]
  const selectedEdges = selectedNode ? data.edges.filter((edge) => edge.sourceNodeId === selectedNode.id || edge.targetNodeId === selectedNode.id) : []
  const sections = evidenceTypes.map((entry) => ({
    ...entry,
    nodes: data.nodes.filter((node) => node.type === entry.type),
  }))
  const topicNode = data.nodes.find((node) => node.type === "topic")
  const graphScale = data.nodes.filter((node) => node.type !== "topic").length

  function submitSearch(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const params = new URLSearchParams({ view: "evidence-graph" })
    if (topic.trim()) params.set("topic", topic.trim())
    if (entity.trim()) params.set("entity", entity.trim())
    if (period !== "all") params.set("period", period)
    if (nodeTypes.length) params.set("nodeTypes", nodeTypes.join(","))
    router.push(`/topics?${params.toString()}`)
  }

  function toggleNodeType(type: EvidenceNodeType) {
    setNodeTypes((current) => (current.includes(type) ? current.filter((item) => item !== type) : [...current, type]))
  }

  return (
    <div className="space-y-8">
      <section className="grid gap-8 py-8 lg:grid-cols-[minmax(0,1fr)_24rem] lg:items-end">
        <div className="min-w-0">
          <p className="mb-4 text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Trends / Evidence</p>
          <h1 className="max-w-5xl text-5xl font-black leading-none tracking-normal text-slate-900 sm:text-6xl lg:text-7xl dark:text-foreground">
            Cross-board Evidence Graph
          </h1>
          <p className="mt-6 max-w-3xl text-base leading-7 text-slate-600 dark:text-muted-foreground">{data.summary.summary}</p>
        </div>

        <form onSubmit={submitSearch} className="rounded-md border border-slate-200 bg-white p-5 shadow-sm dark:border-border dark:bg-card">
          <div className="flex items-center gap-3">
            <span className="flex size-10 shrink-0 items-center justify-center rounded-md bg-slate-950 text-white">
              <Network className="size-5" />
            </span>
            <div>
              <p className="text-xs font-semibold uppercase tracking-normal text-slate-500">当前图谱规模</p>
              <p className="text-2xl font-semibold text-slate-900 dark:text-foreground">{graphScale}</p>
            </div>
          </div>
          <div className="mt-5 grid gap-3">
            <label className="text-xs font-semibold text-slate-500" htmlFor="evidence-topic-search">
              搜索主题
            </label>
            <div className="flex gap-2">
              <input
                id="evidence-topic-search"
                className="min-w-0 flex-1 rounded-md border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 outline-none focus:border-emerald-500 dark:border-border dark:bg-background dark:text-foreground"
                value={topic}
                placeholder="Agent Memory"
                onChange={(event) => setTopic(event.target.value)}
              />
              <button
                type="submit"
                className="inline-flex size-10 shrink-0 items-center justify-center rounded-md bg-slate-950 text-white transition-colors hover:bg-slate-800"
                aria-label="搜索证据图谱"
              >
                <Search className="size-4" />
              </button>
            </div>
            <input
              className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 outline-none focus:border-emerald-500 dark:border-border dark:bg-background dark:text-foreground"
              value={entity}
              placeholder="实体，例如 OpenAI"
              onChange={(event) => setEntity(event.target.value)}
            />
            <select
              className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 dark:border-border dark:bg-background dark:text-foreground"
              value={period}
              onChange={(event) => setPeriod(event.target.value as EvidenceGraphPeriod)}
              aria-label="时间范围"
            >
              {periods.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
          <div className="mt-4 grid grid-cols-2 gap-2">
            {evidenceTypes.map((entry) => (
              <label
                key={entry.type}
                className="flex min-w-0 items-center gap-2 rounded-md border border-slate-200 px-3 py-2 text-xs text-slate-700 dark:border-border dark:text-muted-foreground"
              >
                <input
                  type="checkbox"
                  checked={nodeTypes.includes(entry.type)}
                  onChange={() => toggleNodeType(entry.type)}
                  className="size-3.5"
                />
                <span className="truncate">{entry.label}</span>
              </label>
            ))}
          </div>
        </form>
      </section>

      {data.notices.length ? (
        <section className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-900">
          {data.notices.slice(0, 3).join(" ")}
        </section>
      ) : null}

      <GraphSummary data={data} />

      <section className="grid gap-6 xl:grid-cols-[16rem_minmax(0,1fr)_20rem]">
        <aside className="space-y-4">
          <PanelTitle title="Topic / Entity" />
          <div className="rounded-md border border-slate-200 bg-white p-4 dark:border-border dark:bg-card">
            <p className="break-words text-lg font-semibold text-slate-900 dark:text-foreground">{data.summary.topicName}</p>
            <p className="mt-2 text-sm leading-6 text-slate-600 dark:text-muted-foreground">{trajectoryLabel(data.summary.trajectory)}</p>
            <div className="mt-4 flex flex-wrap gap-2">
              {(topicNode?.tags ?? []).slice(0, 12).map((tag) => (
                <button
                  key={tag}
                  type="button"
                  onClick={() => {
                    setTopic(tag)
                    router.push(`/topics?view=evidence-graph&topic=${encodeURIComponent(tag)}`)
                  }}
                  className="rounded-md border border-slate-200 px-2 py-1 text-xs text-slate-600 transition-colors hover:border-emerald-400 hover:text-emerald-700 dark:border-border dark:text-muted-foreground"
                >
                  {tag}
                </button>
              ))}
            </div>
          </div>
          <div className="space-y-2">
            {sections.map((section) => (
              <button
                key={section.type}
                type="button"
                className="flex w-full items-center justify-between rounded-md border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 dark:border-border dark:bg-card dark:text-foreground"
                onClick={() => section.nodes[0] && setSelectedNodeId(section.nodes[0].id)}
              >
                <span>{section.label}</span>
                <span>{section.nodes.length}</span>
              </button>
            ))}
          </div>
        </aside>

        <main className="min-w-0 space-y-6">
          <EvidenceChain nodes={data.nodes} edges={data.edges} selectedNodeId={selectedNode?.id} onSelect={setSelectedNodeId} />
          <TimelinePanel items={data.timeline} onSelect={setSelectedNodeId} />
        </main>

        <EvidenceInspector node={selectedNode} edges={selectedEdges} nodeById={nodeById} />
      </section>

      <section className="grid gap-4 xl:grid-cols-4">
        {sections.map((section) => (
          <EvidenceColumn key={section.type} title={section.label} nodes={section.nodes} selectedNodeId={selectedNode?.id} onSelect={setSelectedNodeId} />
        ))}
      </section>

      <section className="border-t border-slate-200 pt-8 dark:border-border">
        <PanelTitle title="Related Reports" />
        {data.relatedReports.length ? (
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {data.relatedReports.map((report) => (
              <Link
                key={report.id}
                href={report.href}
                className="rounded-md border border-slate-200 bg-white p-4 transition-colors hover:border-emerald-400 dark:border-border dark:bg-card"
              >
                <p className="text-sm font-semibold text-slate-900 dark:text-foreground">{report.title}</p>
                <p className="mt-2 text-xs text-slate-500">{[report.status, formatDate(report.createdAt)].filter(Boolean).join(" / ")}</p>
                <p className="mt-3 text-sm text-slate-600 dark:text-muted-foreground">{report.evidenceNodeIds.length} 个证据节点关联</p>
              </Link>
            ))}
          </div>
        ) : (
          <EmptyGraphState title="暂无相关报告" description="当前真实报告数据未返回与该主题匹配的报告引用。" />
        )}
      </section>
    </div>
  )
}

function GraphSummary({ data }: { data: EvidenceGraphResponse }) {
  const metrics = [
    { label: "Trend Score", value: data.summary.trendScore, color: "text-emerald-700" },
    { label: "Evidence Score", value: data.summary.evidenceScore, color: "text-sky-700" },
    { label: "Confidence", value: data.summary.confidenceScore, color: "text-violet-700" },
  ]
  const mix = [
    { label: "Papers", value: data.summary.paperCount },
    { label: "Projects", value: data.summary.projectCount },
    { label: "News", value: data.summary.newsCount },
    { label: "Community", value: data.summary.communitySignalCount },
  ]

  return (
    <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_20rem]">
      <div className="rounded-md border border-slate-200 bg-white p-5 dark:border-border dark:bg-card">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase tracking-normal text-slate-500">Graph Summary</p>
            <h2 className="mt-1 break-words text-2xl font-semibold text-slate-900 dark:text-foreground">{data.summary.topicName}</h2>
            <p className="mt-3 max-w-4xl text-sm leading-6 text-slate-600 dark:text-muted-foreground">{data.summary.summary}</p>
          </div>
          <span className="rounded-md border border-slate-200 px-3 py-1 text-sm text-slate-700 dark:border-border dark:text-muted-foreground">
            {trajectoryLabel(data.summary.trajectory)}
          </span>
        </div>
        <div className="mt-5 grid gap-3 md:grid-cols-3">
          {metrics.map((metric) => (
            <div key={metric.label} className="rounded-md border border-slate-200 bg-slate-50 px-4 py-3 dark:border-border dark:bg-background">
              <p className="text-xs text-slate-500">{metric.label}</p>
              <p className={cn("mt-2 text-3xl font-semibold", metric.color)}>{metric.value}</p>
            </div>
          ))}
        </div>
      </div>
      <div className="rounded-md border border-slate-200 bg-white p-5 dark:border-border dark:bg-card">
        <p className="text-xs font-semibold uppercase tracking-normal text-slate-500">Signal Mix</p>
        <div className="mt-4 grid gap-2">
          {mix.map((item) => (
            <div key={item.label} className="flex items-center justify-between rounded-md border border-slate-200 px-3 py-2 dark:border-border">
              <span className="text-sm text-slate-600 dark:text-muted-foreground">{item.label}</span>
              <span className="text-sm font-semibold text-slate-900 dark:text-foreground">{item.value}</span>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}

function EvidenceChain({
  nodes,
  edges,
  selectedNodeId,
  onSelect,
}: {
  nodes: EvidenceNode[]
  edges: EvidenceEdge[]
  selectedNodeId?: string
  onSelect: (nodeId: string) => void
}) {
  const topicNode = nodes.find((node) => node.type === "topic")
  const evidenceNodes = nodes.filter((node) => node.type !== "topic")

  return (
    <section className="rounded-md border border-slate-200 bg-white p-5 dark:border-border dark:bg-card">
      <PanelTitle title="Evidence Chain" />
      {evidenceNodes.length ? (
        <div className="mt-4 grid gap-3">
          {evidenceNodes.slice(0, 14).map((node) => {
            const relation = edges.find((edge) => edge.targetNodeId === node.id || edge.sourceNodeId === node.id)
            return (
              <button
                key={node.id}
                type="button"
                onClick={() => onSelect(node.id)}
                className={cn(
                  "grid gap-3 rounded-md border p-3 text-left transition-colors md:grid-cols-[9rem_minmax(0,1fr)_auto] md:items-center",
                  selectedNodeId === node.id
                    ? "border-emerald-400 bg-emerald-50"
                    : "border-slate-200 bg-white hover:border-emerald-300 dark:border-border dark:bg-background"
                )}
              >
                <span className="inline-flex items-center gap-2 text-xs font-semibold uppercase tracking-normal text-slate-500">
                  {iconForNode(node.type)}
                  {nodeTypeLabels[node.type]}
                </span>
                <span className="min-w-0">
                  <span className="block truncate text-sm font-semibold text-slate-900 dark:text-foreground">{node.title}</span>
                  <span className="mt-1 line-clamp-1 text-xs text-slate-500">{relation?.evidenceText ?? topicNode?.title}</span>
                </span>
                <span className="text-xs text-slate-500">{formatScore(node.confidence)}</span>
              </button>
            )
          })}
        </div>
      ) : (
        <EmptyGraphState title="暂无证据链" description="当前筛选条件下没有真实跨板块证据节点。" />
      )}
    </section>
  )
}

function TimelinePanel({ items, onSelect }: { items: EvidenceGraphTimelineItem[]; onSelect: (nodeId: string) => void }) {
  return (
    <section className="rounded-md border border-slate-200 bg-white p-5 dark:border-border dark:bg-card">
      <PanelTitle title="Timeline" />
      {items.length ? (
        <div className="mt-4 space-y-3">
          {items.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => item.nodeIds[0] && onSelect(item.nodeIds[0])}
              className="grid w-full gap-3 rounded-md border border-slate-200 bg-white p-3 text-left transition-colors hover:border-emerald-300 md:grid-cols-[8rem_minmax(0,1fr)_5rem] md:items-center dark:border-border dark:bg-background"
            >
              <span className="text-xs text-slate-500">{formatDate(item.occurredAt)}</span>
              <span className="min-w-0">
                <span className="block truncate text-sm font-semibold text-slate-900 dark:text-foreground">{item.title}</span>
                <span className="mt-1 line-clamp-1 text-xs text-slate-500">{item.summary}</span>
              </span>
              <span className="text-xs text-slate-500">{item.sourceCount} links</span>
            </button>
          ))}
        </div>
      ) : (
        <EmptyGraphState title="暂无时间线" description="当前证据没有可用时间戳。" />
      )}
    </section>
  )
}

function EvidenceInspector({
  node,
  edges,
  nodeById,
}: {
  node?: EvidenceNode
  edges: EvidenceEdge[]
  nodeById: Map<string, EvidenceNode>
}) {
  if (!node) {
    return (
      <aside className="rounded-md border border-slate-200 bg-white p-5 dark:border-border dark:bg-card">
        <EmptyGraphState title="未选择节点" description="选择一个证据节点查看详情。" />
      </aside>
    )
  }

  const href = hrefForNode(node)
  return (
    <aside className="space-y-4">
      <PanelTitle title="Evidence Inspector" />
      <div className="rounded-md border border-slate-200 bg-white p-5 dark:border-border dark:bg-card">
        <div className="flex items-start justify-between gap-3">
          <span className="inline-flex items-center gap-2 rounded-md border border-slate-200 px-2 py-1 text-xs text-slate-600 dark:border-border dark:text-muted-foreground">
            {iconForNode(node.type)}
            {nodeTypeLabels[node.type]}
          </span>
          <span className="text-xs text-slate-500">{formatScore(node.confidence)}</span>
        </div>
        <h2 className="mt-4 break-words text-lg font-semibold text-slate-900 dark:text-foreground">{node.title}</h2>
        {node.summary ? <p className="mt-3 text-sm leading-6 text-slate-600 dark:text-muted-foreground">{node.summary}</p> : null}
        <dl className="mt-4 grid gap-3 text-sm">
          <InfoRow label="来源" value={node.source ?? "Agora Hub"} />
          <InfoRow label="创建" value={formatDate(node.createdAt)} />
          <InfoRow label="更新" value={formatDate(node.updatedAt)} />
          <InfoRow label="分数" value={formatScore(node.score)} />
        </dl>
        <div className="mt-4 flex flex-wrap gap-2">
          {(node.tags ?? []).slice(0, 8).map((tag) => (
            <span key={tag} className="rounded-md border border-slate-200 px-2 py-1 text-xs text-slate-500 dark:border-border">
              {tag}
            </span>
          ))}
        </div>
        <div className="mt-5 flex flex-wrap gap-2">
          {href ? (
            <Link href={href} className="inline-flex items-center gap-2 rounded-md bg-slate-950 px-3 py-2 text-sm font-semibold text-white">
              打开来源
              <ArrowRight className="size-4" />
            </Link>
          ) : null}
          {node.url ? (
            <a
              href={node.url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-2 rounded-md border border-slate-200 px-3 py-2 text-sm font-semibold text-slate-700 dark:border-border dark:text-muted-foreground"
            >
              原文链接
              <ArrowRight className="size-4" />
            </a>
          ) : null}
        </div>
      </div>
      <div className="rounded-md border border-slate-200 bg-white p-5 dark:border-border dark:bg-card">
        <p className="text-xs font-semibold uppercase tracking-normal text-slate-500">相关边</p>
        {edges.length ? (
          <div className="mt-3 space-y-2">
            {edges.slice(0, 8).map((edge) => {
              const otherNode = nodeById.get(edge.sourceNodeId === node.id ? edge.targetNodeId : edge.sourceNodeId)
              return (
                <div key={edge.id} className="rounded-md border border-slate-200 px-3 py-2 dark:border-border">
                  <p className="text-xs font-semibold text-slate-600 dark:text-muted-foreground">{edgeLabels[edge.type] ?? edge.type}</p>
                  <p className="mt-1 line-clamp-1 text-sm text-slate-900 dark:text-foreground">{otherNode?.title ?? "Graph node"}</p>
                  {edge.evidenceText ? <p className="mt-1 text-xs leading-5 text-slate-500">{edge.evidenceText}</p> : null}
                </div>
              )
            })}
          </div>
        ) : (
          <p className="mt-3 text-sm text-slate-500">暂无相关边。</p>
        )}
      </div>
    </aside>
  )
}

function EvidenceColumn({
  title,
  nodes,
  selectedNodeId,
  onSelect,
}: {
  title: string
  nodes: EvidenceNode[]
  selectedNodeId?: string
  onSelect: (nodeId: string) => void
}) {
  return (
    <article className="rounded-md border border-slate-200 bg-white p-4 dark:border-border dark:bg-card">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-slate-900 dark:text-foreground">{title}</h2>
        <span className="text-sm text-slate-500">{nodes.length}</span>
      </div>
      <div className="mt-3 space-y-2">
        {nodes.length ? (
          nodes.slice(0, 5).map((node) => (
            <button
              key={node.id}
              type="button"
              onClick={() => onSelect(node.id)}
              className={cn(
                "w-full rounded-md border px-3 py-2 text-left transition-colors",
                selectedNodeId === node.id
                  ? "border-emerald-400 bg-emerald-50"
                  : "border-slate-200 hover:border-emerald-300 dark:border-border"
              )}
            >
              <span className="line-clamp-2 text-sm font-medium text-slate-900 dark:text-foreground">{node.title}</span>
              <span className="mt-1 block text-xs text-slate-500">{node.source ?? formatDate(node.updatedAt ?? node.createdAt)}</span>
            </button>
          ))
        ) : (
          <p className="rounded-md border border-dashed border-slate-200 px-3 py-3 text-sm text-slate-500 dark:border-border">暂无真实证据。</p>
        )}
      </div>
    </article>
  )
}

function EmptyGraphState({ title, description }: { title: string; description: string }) {
  return (
    <div className="rounded-md border border-dashed border-slate-200 bg-slate-50 px-4 py-5 text-sm text-slate-600 dark:border-border dark:bg-background dark:text-muted-foreground">
      <p className="font-semibold text-slate-800 dark:text-foreground">{title}</p>
      <p className="mt-1 leading-6">{description}</p>
    </div>
  )
}

function PanelTitle({ title }: { title: string }) {
  return <p className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">{title}</p>
}

function InfoRow({ label, value }: { label: string; value?: string }) {
  return (
    <div className="grid grid-cols-[4rem_minmax(0,1fr)] gap-2">
      <dt className="text-slate-500">{label}</dt>
      <dd className="min-w-0 break-words text-slate-800 dark:text-foreground">{value || "暂无"}</dd>
    </div>
  )
}

function iconForNode(type: EvidenceNodeType) {
  const className = "size-4"
  if (type === "paper" || type === "method" || type === "task") return <BookOpen className={className} />
  if (type === "project") return <GitBranch className={className} />
  if (type === "news") return <Newspaper className={className} />
  if (type === "community_signal") return <MessageSquareText className={className} />
  if (type === "report") return <FileText className={className} />
  return <Check className={className} />
}

function hrefForNode(node: EvidenceNode) {
  const href = node.metadata?.href
  return typeof href === "string" ? href : undefined
}

function formatScore(value?: number) {
  return value === undefined ? "暂无" : `${Math.round(value)}`
}

function formatDate(value?: string) {
  if (!value) return "暂无"
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", year: "numeric" }).format(date)
}

function trajectoryLabel(value: EvidenceGraphResponse["summary"]["trajectory"]) {
  const labels: Record<EvidenceGraphResponse["summary"]["trajectory"], string> = {
    rising: "趋势升温",
    stable: "趋势稳定",
    declining: "趋势退潮",
    noisy: "噪声偏高",
    uncertain: "证据不足",
  }
  return labels[value]
}
