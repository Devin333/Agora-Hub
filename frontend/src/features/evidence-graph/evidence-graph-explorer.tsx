"use client"

import Link from "next/link"
import { useRouter } from "next/navigation"
import { useMemo, useState } from "react"
import { ArrowRight, ArrowUpRight, Network, Search } from "lucide-react"
import { EmptyState } from "@/components/common/empty-state"
import { FilterChip } from "@/components/common/filter-chip"
import { NativeSelect, NativeSelectOption } from "@/components/common/native-select"
import { SearchField } from "@/components/common/search-field"
import { SectionCaption, SectionPanel } from "@/components/common/section-panel"
import { PageHeader } from "@/components/layout/page-header"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { NodeKindIcon, NodeKindMarker, NodeKindPill, nodeTypeLabels } from "@/features/evidence-graph/evidence-node-appearance"
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

/** Shared list recipe: hairline-divided rows inside a rounded frame. */
const listFrameClass = "divide-y divide-border overflow-hidden rounded-xl border border-border"

/** Shared interactive row recipe used by every selectable evidence list. */
function rowClass(selected: boolean) {
  return cn(
    "w-full px-4 py-3 text-left outline-none transition-colors focus-visible:ring-3 focus-visible:ring-inset focus-visible:ring-ring/50",
    selected ? "bg-accent/60" : "hover:bg-accent/40"
  )
}

const fieldClass =
  "h-8 w-full rounded-lg border border-border bg-background px-3 py-1.5 text-[13px] text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"

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
    <div className="space-y-6">
      <div>
        <PageHeader
          eyebrow="Trends / Evidence"
          title="Cross-board Evidence Graph"
          description={data.summary.summary}
          actions={
            <div className="flex items-center gap-2.5 rounded-xl border border-border px-3 py-1.5">
              <Network size={16} strokeWidth={1.8} aria-hidden="true" className="text-muted-foreground" />
              <span className="text-label text-muted-foreground">当前图谱规模</span>
              <span className="text-body1 font-semibold tabular-nums text-foreground">{graphScale}</span>
            </div>
          }
        />

        <form onSubmit={submitSearch} className="flex flex-wrap items-center gap-2 border-b border-border py-3">
          <label className="sr-only" htmlFor="evidence-topic-search">
            搜索主题
          </label>
          <div className="flex w-full max-w-[320px] items-center gap-1.5">
            <SearchField
              id="evidence-topic-search"
              className="flex-1"
              value={topic}
              placeholder="Agent Memory"
              onChange={(event) => setTopic(event.target.value)}
            />
            <Button type="submit" size="icon" aria-label="搜索证据图谱">
              <Search size={15} strokeWidth={1.8} />
            </Button>
          </div>
          <input
            className={cn(fieldClass, "max-w-[220px]")}
            value={entity}
            placeholder="实体，例如 OpenAI"
            aria-label="实体"
            onChange={(event) => setEntity(event.target.value)}
          />
          <NativeSelect value={period} onChange={(event) => setPeriod(event.target.value as EvidenceGraphPeriod)} aria-label="时间范围">
            {periods.map((item) => (
              <NativeSelectOption key={item.value} value={item.value}>
                {item.label}
              </NativeSelectOption>
            ))}
          </NativeSelect>
          <span aria-hidden="true" className="mx-1 hidden h-5 w-px bg-border sm:block" />
          <div role="group" aria-label="证据类型" className="flex flex-wrap items-center gap-1.5">
            {evidenceTypes.map((entry) => (
              <FilterChip key={entry.type} active={nodeTypes.includes(entry.type)} onClick={() => toggleNodeType(entry.type)}>
                {entry.label}
              </FilterChip>
            ))}
          </div>
        </form>
      </div>

      {data.notices.length ? (
        <section className="rounded-xl border border-warning/30 bg-warning/10 px-4 py-3 text-body2 text-foreground">
          {data.notices.slice(0, 3).join(" ")}
        </section>
      ) : null}

      <GraphSummary data={data} />

      <section className="grid gap-4 xl:grid-cols-[16rem_minmax(0,1fr)_20rem]">
        <aside className="min-w-0 space-y-4">
          <SectionPanel title="Topic / Entity">
            <p className="break-words text-[16px] font-semibold text-foreground">{data.summary.topicName}</p>
            <p className="mt-1 text-label text-muted-foreground">{trajectoryLabel(data.summary.trajectory)}</p>
            {topicNode?.tags?.length ? (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {topicNode.tags.slice(0, 12).map((tag) => (
                  <Button
                    key={tag}
                    type="button"
                    variant="outline"
                    size="xs"
                    className="rounded-full text-muted-foreground"
                    onClick={() => {
                      setTopic(tag)
                      router.push(`/topics?view=evidence-graph&topic=${encodeURIComponent(tag)}`)
                    }}
                  >
                    {tag}
                  </Button>
                ))}
              </div>
            ) : null}
          </SectionPanel>
          <nav aria-label="证据分区" className={listFrameClass}>
            {sections.map((section) => {
              const active = section.nodes.some((node) => node.id === selectedNode?.id)
              return (
                <button
                  key={section.type}
                  type="button"
                  className={cn(rowClass(active), "flex items-center justify-between gap-3 text-body2")}
                  onClick={() => section.nodes[0] && setSelectedNodeId(section.nodes[0].id)}
                >
                  <span className="flex min-w-0 items-center gap-2.5 text-foreground">
                    <NodeKindIcon type={section.type} />
                    <span className="truncate">{section.label}</span>
                  </span>
                  <span className="text-label tabular-nums text-muted-foreground">{section.nodes.length}</span>
                </button>
              )
            })}
          </nav>
        </aside>

        <div className="min-w-0 space-y-4">
          <EvidenceChain nodes={data.nodes} edges={data.edges} selectedNodeId={selectedNode?.id} onSelect={setSelectedNodeId} />
          <TimelinePanel items={data.timeline} onSelect={setSelectedNodeId} />
        </div>

        <EvidenceInspector node={selectedNode} edges={selectedEdges} nodeById={nodeById} />
      </section>

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {sections.map((section) => (
          <EvidenceColumn
            key={section.type}
            type={section.type}
            title={section.label}
            nodes={section.nodes}
            selectedNodeId={selectedNode?.id}
            onSelect={setSelectedNodeId}
          />
        ))}
      </section>

      <SectionPanel title="Related Reports" framed={false} className="border-t border-border pt-6">
        {data.relatedReports.length ? (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {data.relatedReports.map((report) => (
              <Link
                key={report.id}
                href={report.href}
                className="group rounded-2xl border border-border bg-card p-4 outline-none transition-colors hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                <span className="flex items-start justify-between gap-3">
                  <span className="min-w-0 text-body1 font-medium text-foreground">{report.title}</span>
                  <ArrowUpRight size={15} strokeWidth={1.8} aria-hidden="true" className="mt-0.5 shrink-0 text-muted-foreground group-hover:text-foreground" />
                </span>
                <span className="mt-1 block text-label text-muted-foreground">{[report.status, formatDate(report.createdAt)].filter(Boolean).join(" / ")}</span>
                <span className="mt-3 block text-body2 text-muted-foreground">{report.evidenceNodeIds.length} 个证据节点关联</span>
              </Link>
            ))}
          </div>
        ) : (
          <EmptyGraphState title="暂无相关报告" description="当前真实报告数据未返回与该主题匹配的报告引用。" />
        )}
      </SectionPanel>
    </div>
  )
}

function GraphSummary({ data }: { data: EvidenceGraphResponse }) {
  const metrics = [
    { label: "Trend Score", value: data.summary.trendScore, tone: "text-success" },
    { label: "Evidence Score", value: data.summary.evidenceScore, tone: "text-link" },
    { label: "Confidence", value: data.summary.confidenceScore, tone: "text-status-working" },
  ]
  const mix = [
    { type: "paper" as const, label: "Papers", value: data.summary.paperCount },
    { type: "project" as const, label: "Projects", value: data.summary.projectCount },
    { type: "news" as const, label: "News", value: data.summary.newsCount },
    { type: "community_signal" as const, label: "Community", value: data.summary.communitySignalCount },
  ]

  return (
    <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_20rem]">
      <div className="rounded-2xl border border-border bg-card p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <SectionCaption>Graph Summary</SectionCaption>
            <h2 className="mt-0.5 break-words text-heading3 font-semibold text-foreground">{data.summary.topicName}</h2>
            <p className="mt-1.5 max-w-4xl text-body2 text-muted-foreground">{data.summary.summary}</p>
          </div>
          <Badge variant={trajectoryBadgeVariant(data.summary.trajectory)}>{trajectoryLabel(data.summary.trajectory)}</Badge>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-3">
          {metrics.map((metric) => (
            <div key={metric.label} className="rounded-xl border border-border px-4 py-3">
              <p className="text-label text-muted-foreground">{metric.label}</p>
              <p className={cn("mt-1 text-heading1 font-semibold tabular-nums", metric.tone)}>{metric.value}</p>
            </div>
          ))}
        </div>
      </div>
      <SectionPanel title="Signal Mix">
        <div className={listFrameClass}>
          {mix.map((item) => (
            <div key={item.label} className="flex items-center justify-between gap-3 px-4 py-2.5">
              <span className="flex min-w-0 items-center gap-2.5 text-body2 text-muted-foreground">
                <NodeKindIcon type={item.type} />
                <span className="truncate">{item.label}</span>
              </span>
              <span className="text-body2 font-semibold tabular-nums text-foreground">{item.value}</span>
            </div>
          ))}
        </div>
      </SectionPanel>
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
    <SectionPanel title="Evidence Chain" description={evidenceNodes.length ? `${Math.min(evidenceNodes.length, 14)} / ${evidenceNodes.length}` : undefined}>
      {evidenceNodes.length ? (
        <div className={listFrameClass}>
          {evidenceNodes.slice(0, 14).map((node) => {
            const relation = edges.find((edge) => edge.targetNodeId === node.id || edge.sourceNodeId === node.id)
            const selected = selectedNodeId === node.id
            return (
              <button
                key={node.id}
                type="button"
                aria-pressed={selected}
                onClick={() => onSelect(node.id)}
                className={cn(rowClass(selected), "grid gap-3 md:grid-cols-[8.5rem_minmax(0,1fr)_auto] md:items-center")}
              >
                <span className="flex items-center gap-2.5">
                  <NodeKindMarker type={node.type} />
                  <span className="text-label font-medium text-muted-foreground">{nodeTypeLabels[node.type]}</span>
                </span>
                <span className="min-w-0">
                  <span className="block truncate text-body2 font-medium text-foreground">{node.title}</span>
                  <span className="mt-0.5 line-clamp-1 text-label text-muted-foreground">{relation?.evidenceText ?? topicNode?.title}</span>
                </span>
                <span className="text-label tabular-nums text-muted-foreground">{formatScore(node.confidence)}</span>
              </button>
            )
          })}
        </div>
      ) : (
        <EmptyGraphState title="暂无证据链" description="当前筛选条件下没有真实跨板块证据节点。" />
      )}
    </SectionPanel>
  )
}

function TimelinePanel({ items, onSelect }: { items: EvidenceGraphTimelineItem[]; onSelect: (nodeId: string) => void }) {
  return (
    <SectionPanel title="Timeline">
      {items.length ? (
        <ol className={listFrameClass}>
          {items.map((item) => (
            <li key={item.id}>
              <button
                type="button"
                onClick={() => item.nodeIds[0] && onSelect(item.nodeIds[0])}
                className={cn(rowClass(false), "grid gap-3 md:grid-cols-[7.5rem_minmax(0,1fr)_5rem] md:items-center")}
              >
                <span className="text-label tabular-nums text-muted-foreground">{formatDate(item.occurredAt)}</span>
                <span className="min-w-0">
                  <span className="block truncate text-body2 font-medium text-foreground">{item.title}</span>
                  <span className="mt-0.5 line-clamp-1 text-label text-muted-foreground">{item.summary}</span>
                </span>
                <span className="text-label tabular-nums text-muted-foreground md:text-right">{item.sourceCount} links</span>
              </button>
            </li>
          ))}
        </ol>
      ) : (
        <EmptyGraphState title="暂无时间线" description="当前证据没有可用时间戳。" />
      )}
    </SectionPanel>
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
      <aside className="min-w-0 rounded-2xl border border-border bg-card p-4">
        <EmptyGraphState title="未选择节点" description="选择一个证据节点查看详情。" />
      </aside>
    )
  }

  const href = hrefForNode(node)
  return (
    <aside className="min-w-0 space-y-4" aria-label="Evidence Inspector">
      <div className="overflow-hidden rounded-2xl border border-border bg-card">
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <h2 className="text-[15px] font-semibold text-foreground">Evidence Inspector</h2>
          <span className="text-label tabular-nums text-muted-foreground">{formatScore(node.confidence)}</span>
        </div>
        <div className="p-4">
          <NodeKindPill type={node.type} />
          <h3 className="mt-3 break-words text-[16px] font-semibold text-foreground">{node.title}</h3>
          {node.summary ? <p className="mt-1.5 text-body2 text-muted-foreground">{node.summary}</p> : null}
          <dl className="mt-4 divide-y divide-border rounded-xl border border-border text-body2">
            <InfoRow label="来源" value={node.source ?? "Agora Hub"} />
            <InfoRow label="创建" value={formatDate(node.createdAt)} />
            <InfoRow label="更新" value={formatDate(node.updatedAt)} />
            <InfoRow label="分数" value={formatScore(node.score)} />
          </dl>
          {node.tags?.length ? (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {node.tags.slice(0, 8).map((tag) => (
                <Badge key={tag} variant="outline" className="font-normal text-muted-foreground">
                  {tag}
                </Badge>
              ))}
            </div>
          ) : null}
          {href || node.url ? (
            <div className="mt-4 flex flex-wrap gap-2">
              {href ? (
                <Button asChild size="sm">
                  <Link href={href}>
                    打开来源
                    <ArrowRight size={15} strokeWidth={1.8} />
                  </Link>
                </Button>
              ) : null}
              {node.url ? (
                <Button asChild size="sm" variant="outline">
                  <a href={node.url} target="_blank" rel="noreferrer">
                    原文链接
                    <ArrowUpRight size={15} strokeWidth={1.8} />
                  </a>
                </Button>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
      <SectionPanel title="相关边" headingLevel={3}>
        {edges.length ? (
          <ul className={listFrameClass}>
            {edges.slice(0, 8).map((edge) => {
              const otherNode = nodeById.get(edge.sourceNodeId === node.id ? edge.targetNodeId : edge.sourceNodeId)
              return (
                <li key={edge.id} className="px-4 py-3">
                  <p className="text-label font-medium text-link">{edgeLabels[edge.type] ?? edge.type}</p>
                  <p className="mt-0.5 line-clamp-1 text-body2 text-foreground">{otherNode?.title ?? "Graph node"}</p>
                  {edge.evidenceText ? <p className="mt-0.5 text-label text-muted-foreground">{edge.evidenceText}</p> : null}
                </li>
              )
            })}
          </ul>
        ) : (
          <p className="text-body2 text-muted-foreground">暂无相关边。</p>
        )}
      </SectionPanel>
    </aside>
  )
}

function EvidenceColumn({
  type,
  title,
  nodes,
  selectedNodeId,
  onSelect,
}: {
  type: EvidenceNodeType
  title: string
  nodes: EvidenceNode[]
  selectedNodeId?: string
  onSelect: (nodeId: string) => void
}) {
  return (
    <article className="min-w-0 rounded-2xl border border-border bg-card p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="flex min-w-0 items-center gap-2 text-[15px] font-semibold text-foreground">
          <NodeKindIcon type={type} />
          <span className="truncate">{title}</span>
        </h2>
        <span className="text-label tabular-nums text-muted-foreground">{nodes.length}</span>
      </div>
      {nodes.length ? (
        <div className={listFrameClass}>
          {nodes.slice(0, 5).map((node) => {
            const selected = selectedNodeId === node.id
            return (
              <button key={node.id} type="button" aria-pressed={selected} onClick={() => onSelect(node.id)} className={rowClass(selected)}>
                <span className="line-clamp-2 text-body2 font-medium text-foreground">{node.title}</span>
                <span className="mt-0.5 block truncate text-label text-muted-foreground">{node.source ?? formatDate(node.updatedAt ?? node.createdAt)}</span>
              </button>
            )
          })}
        </div>
      ) : (
        <p className="rounded-xl border border-dashed border-border px-4 py-3 text-body2 text-muted-foreground">暂无真实证据。</p>
      )}
    </article>
  )
}

function EmptyGraphState({ title, description }: { title: string; description: string }) {
  return (
    <div className="rounded-xl border border-dashed border-border">
      <EmptyState title={title} description={description} />
    </div>
  )
}

function InfoRow({ label, value }: { label: string; value?: string }) {
  return (
    <div className="grid grid-cols-[4rem_minmax(0,1fr)] gap-2 px-4 py-2.5">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words text-foreground">{value || "暂无"}</dd>
    </div>
  )
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

type Trajectory = EvidenceGraphResponse["summary"]["trajectory"]

function trajectoryLabel(value: Trajectory) {
  const labels: Record<Trajectory, string> = {
    rising: "趋势升温",
    stable: "趋势稳定",
    declining: "趋势退潮",
    noisy: "噪声偏高",
    uncertain: "证据不足",
  }
  return labels[value]
}

function trajectoryBadgeVariant(value: Trajectory) {
  const variants: Record<Trajectory, "success" | "info" | "danger" | "warning" | "muted"> = {
    rising: "success",
    stable: "info",
    declining: "danger",
    noisy: "warning",
    uncertain: "muted",
  }
  return variants[value]
}
