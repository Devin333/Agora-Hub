import type {
  AgentRun,
  AgentRunDetail,
  AgentStep,
  Artifact,
  MemoryHit,
  RunErrorTrace,
  RunLogItem,
  RunQualitySummary,
  ToolCall,
  WorkflowDagEdge,
  WorkflowDagNode
} from "@/types/agent"

const baseTime = Date.parse("2026-05-22T08:00:00.000Z")

function iso(minutes: number): string {
  return new Date(baseTime + minutes * 60_000).toISOString()
}

export const studioAgentRuns: AgentRun[] = [
  run("run-daily-20260522-0800", "DailyNewsAgent", "每日 NewsRoom 简报", "离线 Agent", "partially_failed", 0, 54, 126, 38, 8, 5, 72, 1),
  run("run-topic-20260522-0730", "TopicClusterAgent", "主题簇刷新", "默认", "success", -30, 22, 84, 12, 7, 3, 91, 0),
  run("run-report-20260522-0715", "ReportWriterAgent", "每周情报报告", "编辑", "running", -45, undefined, 42, 8, 5, 2, 64, 0),
  run("run-verify-20260522-0700", "VerificationAgent", "证据验证", "严格", "failed", -60, -45, 18, 0, 6, 1, 38, 2),
  run("run-trend-20260522-0630", "TrendHunterAgent", "趋势扫描", "雷达", "success", -90, -72, 63, 11, 7, 3, 87, 0),
  run("run-daily-20260521-0800", "DailyNewsAgent", "每日 NewsRoom 简报", "离线 Agent", "success", -1440, -1388, 118, 41, 8, 6, 89, 0),
  run("run-topic-20260521-0730", "TopicClusterAgent", "主题簇刷新", "默认", "failed", -1470, -1458, 57, 0, 4, 1, 42, 1),
  run("run-report-20260521-0715", "ReportWriterAgent", "每日读者报告", "编辑", "success", -1485, -1450, 32, 6, 6, 4, 81, 0),
  run("run-verify-20260521-0700", "VerificationAgent", "证据验证", "严格", "success", -1500, -1486, 20, 4, 5, 2, 94, 0),
  run("run-trend-20260521-0630", "TrendHunterAgent", "趋势扫描", "雷达", "success", -1530, -1511, 71, 13, 7, 4, 85, 0),
  run("run-memory-20260521-0600", "MemoryRecallAgent", "记忆召回刷新", "召回 v2", "success", -1560, -1544, 44, 9, 5, 2, 79, 0),
  run("run-report-20260520-2200", "ReportWriterAgent", "夜间报告", "夜间", "cancelled", -2040, -2031, 16, 0, 3, 0, undefined, 0)
]

const completeRunId = studioAgentRuns[0].id

const steps: AgentStep[] = [
  {
    id: "step-collect",
    runId: completeRunId,
    nodeId: "node-collect",
    label: "采集来源信号",
    type: "collect",
    status: "success",
    startedAt: iso(0),
    finishedAt: iso(8),
    durationMs: 480000,
    inputPreview: { sources: ["OpenAI Blog", "Hacker News", "GitHub Trending"], targetWindow: "24h" },
    outputPreview: { items: 126, acceptedSources: 14, rejectedSources: 2 },
    artifactIds: ["artifact-source-snapshot"]
  },
  {
    id: "step-process",
    runId: completeRunId,
    nodeId: "node-process",
    label: "标准化与去重",
    type: "process",
    status: "success",
    startedAt: iso(8),
    finishedAt: iso(16),
    durationMs: 480000,
    inputPreview: { rawItems: 126 },
    outputPreview: { normalizedItems: 104, duplicateGroups: 18 }
  },
  {
    id: "step-rank",
    runId: completeRunId,
    nodeId: "node-rank",
    label: "排序情报候选",
    type: "rank",
    status: "success",
    startedAt: iso(16),
    finishedAt: iso(23),
    durationMs: 420000,
    inputPreview: { normalizedItems: 104, scoringProfile: "daily-v3" },
    outputPreview: { selectedItems: 38, topTopic: "Agent 运行时可观测性" }
  },
  {
    id: "step-memory",
    runId: completeRunId,
    nodeId: "node-memory",
    label: "召回记忆上下文",
    type: "memory",
    status: "success",
    startedAt: iso(23),
    finishedAt: iso(29),
    durationMs: 360000,
    inputPreview: { queryCount: 8, collections: ["topics", "reports", "agent_notes"] },
    outputPreview: { hits: 7, appliedHits: 5 }
  },
  {
    id: "step-research",
    runId: completeRunId,
    nodeId: "node-research",
    label: "补充缺失证据",
    type: "research",
    status: "success",
    startedAt: iso(29),
    finishedAt: iso(38),
    durationMs: 540000,
    inputPreview: { gaps: ["厂商确认", "发布时间线", "基准来源"] },
    outputPreview: { resolvedGaps: 2, unresolvedGaps: 1 }
  },
  {
    id: "step-report",
    runId: completeRunId,
    nodeId: "node-report",
    label: "起草每日报告",
    type: "report",
    status: "success",
    startedAt: iso(38),
    finishedAt: iso(48),
    durationMs: 600000,
    inputPreview: { sections: ["导语", "信号", "观察列表"], memoryHits: 5 },
    outputPreview: { markdownBytes: 18432, citations: 21 },
    artifactIds: ["artifact-daily-report-md", "artifact-report-json"]
  },
  {
    id: "step-quality",
    runId: completeRunId,
    nodeId: "node-quality",
    label: "运行质量门控",
    type: "quality",
    status: "failed",
    startedAt: iso(48),
    finishedAt: iso(53),
    durationMs: 300000,
    inputPreview: { citationCoverage: 0.74, unresolvedGaps: 1 },
    outputPreview: { score: 72, status: "review_required", failedChecks: ["引用覆盖"] },
    errorMessage: "一个高影响声明的引用覆盖低于严格阈值。",
    artifactIds: ["artifact-quality-summary"]
  },
  {
    id: "step-final",
    runId: completeRunId,
    nodeId: "node-final",
    label: "发布运行清单",
    type: "final",
    status: "skipped",
    startedAt: iso(53),
    finishedAt: iso(54),
    durationMs: 60000,
    inputPreview: { gateStatus: "review_required" },
    outputPreview: { published: false, reason: "质量门控需要复核" }
  }
]

const dagNodes: WorkflowDagNode[] = steps.map((step) => ({
  id: step.nodeId,
  stepId: step.id,
  label: step.label,
  type: step.type,
  status: step.status,
  durationMs: step.durationMs,
  inputCount: step.inputPreview ? 1 : 0,
  outputCount: step.outputPreview ? 1 : 0,
  errorMessage: step.errorMessage
}))

const dagEdges: WorkflowDagEdge[] = [
  edge("node-collect", "node-process"),
  edge("node-process", "node-rank"),
  edge("node-rank", "node-memory"),
  edge("node-memory", "node-research"),
  edge("node-research", "node-report"),
  edge("node-report", "node-quality"),
  edge("node-quality", "node-final")
]

const artifacts: Artifact[] = [
  {
    id: "artifact-source-snapshot",
    runId: completeRunId,
    stepId: "step-collect",
    artifactType: "json",
    filename: "source-snapshot.json",
    sizeBytes: 48219,
    createdAt: iso(8),
    preview: JSON.stringify({ sourceCount: 14, itemCount: 126, rejectedSources: ["过期 RSS", "冷却来源"] }, null, 2)
  },
  {
    id: "artifact-daily-report-md",
    runId: completeRunId,
    stepId: "step-report",
    artifactType: "markdown",
    filename: "daily-agent-report.md",
    sizeBytes: 18432,
    createdAt: iso(48),
    preview: "# 每日新闻情报\n\nAgent 运行时可观测性仍是最强运营信号。来源覆盖较广，但一个高影响声明在发布前需要引用复核。"
  },
  {
    id: "artifact-report-json",
    runId: completeRunId,
    stepId: "step-report",
    artifactType: "json",
    filename: "report.json",
    sizeBytes: 12684,
    createdAt: iso(48),
    preview: JSON.stringify({ title: "每日新闻情报", sections: 5, citations: 21, memoryHits: 5 }, null, 2)
  },
  {
    id: "artifact-quality-summary",
    runId: completeRunId,
    stepId: "step-quality",
    artifactType: "json",
    filename: "quality-summary.json",
    sizeBytes: 3921,
    createdAt: iso(53),
    preview: JSON.stringify({ score: 72, status: "review_required", failedChecks: ["引用覆盖"] }, null, 2)
  },
  {
    id: "artifact-debug-log",
    runId: completeRunId,
    stepId: "step-quality",
    artifactType: "log",
    filename: "quality-debug.log",
    sizeBytes: 8210,
    createdAt: iso(53),
    preview: "quality.check citation_coverage failed threshold=82 actual=74 claim_id=claim-runtime-market-size"
  }
]

const quality: RunQualitySummary = {
  runId: completeRunId,
  score: 72,
  status: "review_required",
  checks: [
    { id: "check-citations", name: "引用覆盖", status: "failed", message: "一个高影响声明缺少足够来源支撑。" },
    { id: "check-freshness", name: "新鲜度", status: "passed", message: "所有重点条目都在配置的 24h 窗口内。" },
    { id: "check-source-diversity", name: "来源多样性", status: "warning", message: "主导主题中社区来源多于一手来源。" },
    { id: "check-memory-grounding", name: "记忆依据", status: "passed", message: "历史上下文已应用到三个章节。" },
    { id: "check-redaction", name: "敏感数据脱敏", status: "passed", message: "预览中未发现敏感密钥。" },
    { id: "check-report-shape", name: "报告结构", status: "passed", message: "必需章节均已存在。" }
  ]
}

const errors: RunErrorTrace[] = [
  {
    id: "err-quality-citation",
    runId: completeRunId,
    stepId: "step-quality",
    timestamp: iso(52),
    message: "一个高影响声明的引用覆盖低于严格阈值。",
    stackPreview: "QualityGateError: citation coverage below threshold\n  at runCitationCoverageCheck\n  at evaluateQualityGate\n  at executeStep(step-quality)",
    retryHint: "复核 claim-runtime-market-size，补充一手来源，然后从质量步骤重新运行。"
  }
]

const toolCalls: ToolCall[] = [
  tool("tool-fetch-openai", "step-collect", "source.fetch", "success", 1, 2600, { source: "OpenAI Blog" }, { items: 8 }),
  tool("tool-fetch-hn", "step-collect", "source.fetch", "success", 3, 1900, { source: "Hacker News" }, { items: 42 }),
  tool("tool-recall-topic", "step-memory", "memory.recall", "success", 24, 870, { query: "Agent 运行时可观测性" }, { hits: 5 }),
  tool("tool-evidence-search", "step-research", "evidence.search", "success", 31, 1450, { gap: "厂商确认" }, { evidenceIds: ["ev-441", "ev-442"] }),
  tool("tool-quality-check", "step-quality", "quality.evaluate", "failed", 49, 4120, { threshold: 82 }, undefined, "引用覆盖低于阈值。"),
  tool("tool-artifact-write", "step-report", "artifact.write", "success", 47, 530, { artifact: "daily-agent-report.md" }, { bytes: 18432 })
]

const memoryHits: MemoryHit[] = [
  memory("mem-topic-001", "topic", "step-memory", 94, "此前的 Agent 运行时可观测性主题簇，包含 LangSmith 和工作流 trace 引用。", "Agent 运行时可观测性", "ev-221"),
  memory("mem-report-014", "report", "step-memory", 88, "上一份周报记录了运营者对可回放 Agent 轨迹的需求上升。", "Agent 运行时可观测性"),
  memory("mem-evidence-443", "evidence", "step-memory", 81, "此前用于讨论 trace 开销的基准来源。", "推理基础设施", "ev-443"),
  memory("mem-note-020", "agent_note", "step-memory", 79, "复核偏好：没有一手引用时，不发布市场规模声明。", "编辑守则"),
  memory("mem-entity-009", "entity", "step-memory", 75, "用于可观测性平台对比的厂商实体画像。", "开发者工具")
]

const logs: RunLogItem[] = Array.from({ length: 24 }, (_, index) => {
  const step = steps[Math.min(Math.floor(index / 3), steps.length - 1)]
  const level = index === 20 ? "error" : index % 7 === 0 ? "warning" : index % 5 === 0 ? "debug" : "info"
  return {
    id: `log-${String(index + 1).padStart(2, "0")}`,
    timestamp: iso(index * 2),
    level,
    message:
      index === 20
        ? "质量门控拒绝了一个高影响声明，因为引用覆盖低于阈值。"
        : `${step.label}：${level === "debug" ? "已捕获调试信号" : "已记录进度检查点"}`,
    stepId: step.id
  }
})

export const studioAgentRunDetails: Record<string, AgentRunDetail> = {
  [completeRunId]: {
    run: studioAgentRuns[0],
    steps,
    dag: { nodes: dagNodes, edges: dagEdges },
    logs,
    toolCalls,
    memoryHits,
    artifacts,
    quality,
    errors,
    dataState: "ready",
    notices: ["已加载这次运行的完整 Studio mock 可观测性详情。"]
  }
}

export function fallbackDetailForRun(runItem: AgentRun): AgentRunDetail {
  const template = studioAgentRunDetails[completeRunId]
  const clonedSteps = template.steps.map((step) => ({
    ...step,
    id: step.id.replace("step-", `${runItem.id}-step-`),
    runId: runItem.id,
    nodeId: step.nodeId.replace("node-", `${runItem.id}-node-`),
    status: runItem.status === "failed" && step.type === "quality" ? "failed" : step.status
  }))
  const nodeByOriginal = new Map(template.steps.map((step, index) => [step.nodeId, clonedSteps[index].nodeId]))

  return {
    run: runItem,
    steps: clonedSteps,
    dag: {
      nodes: template.dag.nodes.map((node, index) => ({
        ...node,
        id: clonedSteps[index].nodeId,
        stepId: clonedSteps[index].id,
        status: clonedSteps[index].status,
        errorMessage: clonedSteps[index].errorMessage
      })),
      edges: template.dag.edges.map((item) => ({
        ...item,
        id: `${runItem.id}-${item.id}`,
        source: nodeByOriginal.get(item.source) ?? item.source,
        target: nodeByOriginal.get(item.target) ?? item.target
      }))
    },
    logs: template.logs.slice(0, 12).map((log) => ({ ...log, id: `${runItem.id}-${log.id}`, stepId: clonedSteps[0].id, timestamp: runItem.startedAt })),
    toolCalls: template.toolCalls.slice(0, 3).map((call) => ({ ...call, id: `${runItem.id}-${call.id}`, runId: runItem.id, stepId: clonedSteps[0].id })),
    memoryHits: template.memoryHits.slice(0, 3).map((hit) => ({ ...hit, id: `${runItem.id}-${hit.id}`, runId: runItem.id, stepId: clonedSteps[3].id })),
    artifacts: template.artifacts.slice(0, Math.max(1, Math.min(runItem.artifactCount, 3))).map((artifact) => ({
      ...artifact,
      id: `${runItem.id}-${artifact.id}`,
      runId: runItem.id,
      stepId: clonedSteps[5].id
    })),
    quality: {
      ...template.quality,
      runId: runItem.id,
      score: runItem.qualityScore,
      status: runItem.errorCount > 0 ? "warning" : "passed",
      checks: template.quality?.checks ?? []
    },
    errors:
      runItem.errorCount > 0
        ? template.errors.map((error) => ({ ...error, id: `${runItem.id}-${error.id}`, runId: runItem.id, stepId: clonedSteps[6].id }))
        : [],
    dataState: "fallback",
    notices: ["确定性兜底详情已补齐 API 尚未提供的可观测性字段。"]
  }
}

function run(
  id: string,
  agentName: string,
  workflowName: string,
  profile: string,
  status: AgentRun["status"],
  startMinute: number,
  finishMinute: number | undefined,
  inputCount: number,
  outputCount: number,
  stepCount: number,
  artifactCount: number,
  qualityScore: number | undefined,
  errorCount: number
): AgentRun {
  const startedAt = iso(startMinute)
  const finishedAt = finishMinute === undefined ? undefined : iso(finishMinute)
  const durationMs = Math.max(0, ((finishMinute ?? 0) - startMinute) * 60_000)
  return {
    id,
    agentName,
    workflowName,
    profile,
    status,
    startedAt,
    finishedAt,
    durationMs,
    durationSeconds: Math.round(durationMs / 1000),
    inputCount,
    outputCount,
    stepCount,
    artifactCount,
    qualityScore,
    errorCount,
    steps: [
      { id: "collect", label: "采集", status: status === "pending" ? "pending" : "success" },
      { id: "analyze", label: "分析", status: status === "running" ? "running" : status === "failed" ? "failed" : "success" },
      { id: "publish", label: "发布", status: status === "cancelled" ? "skipped" : status === "failed" ? "skipped" : "success" }
    ]
  }
}

function edge(source: string, target: string): WorkflowDagEdge {
  return { id: `${source}-${target}`, source, target }
}

function tool(
  id: string,
  stepId: string,
  toolName: string,
  status: ToolCall["status"],
  startMinute: number,
  durationMs: number,
  argsPreview?: unknown,
  resultPreview?: unknown,
  errorMessage?: string
): ToolCall {
  return {
    id,
    runId: completeRunId,
    stepId,
    toolName,
    status,
    startedAt: iso(startMinute),
    finishedAt: iso(startMinute + Math.max(1, Math.round(durationMs / 60_000))),
    durationMs,
    argsPreview,
    resultPreview,
    errorMessage
  }
}

function memory(
  memoryId: string,
  memoryType: MemoryHit["memoryType"],
  stepId: string,
  score: number,
  summary: string,
  relatedTopicName?: string,
  relatedEvidenceId?: string
): MemoryHit {
  return {
    id: `${memoryId}-hit`,
    runId: completeRunId,
    stepId,
    memoryId,
    memoryType,
    score,
    summary,
    relatedTopicName,
    relatedEvidenceId,
    createdAt: iso(22)
  }
}
