"use client"

import { useEffect, useRef, useState } from "react"
import Link from "next/link"
import { ArrowLeft, Check, Download, Save } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  downloadResearchReport,
  fetchResearchReport,
  fetchSavedResearchReport,
  saveResearchReport,
} from "@/lib/research/desktop/client"
import {
  toResearchReportView,
  type ResearchReportView,
} from "@/lib/research/desktop/contracts"
import { saveResearchReportReference } from "@/lib/research/history"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"

export function DesktopSavedReportDetail({ reportId }: { reportId: string }) {
  const workspace = useOwnedResearchWorkspace()
  if (!workspace.ready || !workspace.owner) {
    return <PaneStatus>正在核对报告访问身份…</PaneStatus>
  }
  return (
    <OwnedSavedReport
      key={`${workspace.owner}:${reportId}`}
      owner={workspace.owner}
      reportId={reportId}
    />
  )
}

function OwnedSavedReport({ owner, reportId }: { owner: string; reportId: string }) {
  const workspace = useOwnedResearchWorkspace()
  const [report, setReport] = useState<ResearchReportView | null>(null)
  const [error, setError] = useState("")
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setReport(null)
    setError("")
    fetchSavedResearchReport(reportId, controller.signal)
      .then(saved => {
        if (workspace.owner === owner) {
          setReport(toResearchReportView(saved.report, saved.reference))
        }
      })
      .catch(cause => {
        if (!controller.signal.aborted && workspace.owner === owner) {
          setError(cause instanceof Error ? cause.message : "报告暂时不可用。")
        }
      })
    return () => controller.abort()
  }, [attempt, owner, reportId, workspace.owner])

  return (
    <ReportView
      report={report}
      error={error}
      onRetry={() => setAttempt(value => value + 1)}
    />
  )
}

export function DesktopRunReportDetail({
  runId,
  sourceRevision,
}: {
  runId: string
  sourceRevision: string
}) {
  const workspace = useOwnedResearchWorkspace()
  if (!workspace.ready || !workspace.owner) {
    return <PaneStatus>正在核对报告访问身份…</PaneStatus>
  }
  return (
    <OwnedRunReport
      key={`${workspace.owner}:${runId}:${sourceRevision}`}
      owner={workspace.owner}
      runId={runId}
      sourceRevision={sourceRevision}
    />
  )
}

function OwnedRunReport({
  owner,
  runId,
  sourceRevision,
}: {
  owner: string
  runId: string
  sourceRevision: string
}) {
  const workspace = useOwnedResearchWorkspace()
  const [report, setReport] = useState<ResearchReportView | null>(null)
  const [error, setError] = useState("")
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setReport(null)
    setError("")
    setSaved(false)
    fetchResearchReport(runId, sourceRevision, controller.signal)
      .then(value => {
        if (workspace.owner === owner) setReport(toResearchReportView(value))
      })
      .catch(cause => {
        if (!controller.signal.aborted && workspace.owner === owner) {
          setError(cause instanceof Error ? cause.message : "运行报告暂时不可用。")
        }
      })
    return () => controller.abort()
  }, [attempt, owner, runId, sourceRevision, workspace.owner])

  async function save() {
    if (!report || workspace.owner !== owner || saving || !report.acceptedExport) return
    setSaving(true)
    setError("")
    try {
      const reportId = `report-${report.runId}`
        .replace(/[^A-Za-z0-9:._-]/g, "-")
        .slice(0, 128)
      const savedReport = await saveResearchReport({
        reportId,
        runId: report.runId,
        sourceRevision: report.sourceRevision,
        title: report.title,
      })
      if (workspace.owner !== owner) return
      const view = toResearchReportView(savedReport.report, savedReport.reference)
      setReport(view)
      setSaved(true)
      if (
        !saveResearchReportReference(
          {
            id: savedReport.reference.reportId,
            reportId: savedReport.reference.reportId,
            runId: savedReport.reference.runId,
            kind: "accepted",
            title: savedReport.reference.title,
            savedAt: Date.parse(savedReport.reference.savedAt) || Date.now(),
          },
          owner,
        )
      ) {
        setError("报告已保存到服务端，但工作区索引尚未同步。")
      }
    } catch (cause) {
      if (workspace.owner === owner) {
        setError(cause instanceof Error ? cause.message : "保存报告失败。")
      }
    } finally {
      if (workspace.owner === owner) setSaving(false)
    }
  }

  return (
    <ReportView
      report={report}
      error={error}
      save={report?.acceptedExport ? { busy: saving, saved, run: save } : undefined}
      onRetry={() => setAttempt(value => value + 1)}
    />
  )
}

function ReportView({
  report,
  error,
  save,
  onRetry,
}: {
  report: ResearchReportView | null
  error: string
  save?: { busy: boolean; saved: boolean; run: () => Promise<void> }
  onRetry?: () => void
}) {
  if (error && !report) {
    return (
      <div className="px-4 py-10 md:px-6">
        <div role="alert" className="mx-auto flex max-w-[60rem] flex-wrap items-center gap-3 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-body2 text-destructive-text">
          <span>{error}</span>
          {onRetry && (
            <Button type="button" variant="outline" size="sm" onClick={onRetry}>
              重新读取报告
            </Button>
          )}
        </div>
      </div>
    )
  }
  if (!report) return <PaneStatus>正在读取精确运行报告…</PaneStatus>

  return (
    <main className="flex min-h-full flex-col">
      <header className="border-b px-4 py-4 md:px-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <Badge variant={report.acceptedExport ? "success" : "warning"}>
              {report.acceptedExport ? "已通过质量门，可保存与导出" : reportStatusLabel(report)}
            </Badge>
            <h1 className="mt-2 text-heading2 font-semibold">{report.title}</h1>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {save && (
              <Button type="button" disabled={save.busy || save.saved} onClick={() => void save.run()}>
                {save.saved ? <Check size={15} strokeWidth={1.8} /> : <Save size={15} strokeWidth={1.8} />}
                {save.saved ? "已保存" : save.busy ? "正在保存…" : "保存精确运行报告"}
              </Button>
            )}
            {report.acceptedExport && <ReportDownloads report={report} />}
            <Button asChild variant="ghost">
              <Link href="/reports">
                <ArrowLeft size={15} strokeWidth={1.8} />
                返回报告目录
              </Link>
            </Button>
          </div>
        </div>
        {error && <p role="alert" className="mt-3 text-body2 text-destructive-text">{error}</p>}
        <details className="mt-3 text-label text-muted-foreground">
          <summary className="cursor-pointer hover:text-foreground">技术信息</summary>
          <dl className="mt-2 grid gap-1 break-all">
            <div><dt className="inline font-medium">运行：</dt><dd className="inline">{report.runId}</dd></div>
            <div><dt className="inline font-medium">来源修订：</dt><dd className="inline">{report.sourceRevision}</dd></div>
            <div><dt className="inline font-medium">运行状态：</dt><dd className="inline">{report.status}</dd></div>
            <div><dt className="inline font-medium">产物状态：</dt><dd className="inline">{report.artifactStatus}</dd></div>
          </dl>
        </details>
      </header>

      <div className="mx-auto w-full max-w-[60rem] space-y-8 px-4 py-6 md:px-6">
        <ReportSection title="质量判定">
          <div className="divide-y rounded-xl border">
            <div className="flex items-center justify-between gap-3 px-4 py-3 text-body2">
              <span className="text-muted-foreground">判定</span>
              <Badge variant={report.qualityPassed ? "success" : "danger"}>{qualityLabel(report)}</Badge>
            </div>
            {report.quality.score != null && (
              <div className="flex items-center justify-between gap-3 px-4 py-3 text-body2">
                <span className="text-muted-foreground">质量评分</span>
                <span className="font-medium tabular-nums">评分：{report.quality.score}</span>
              </div>
            )}
            {report.quality.reasons.length > 0 && (
              <ul className="list-disc space-y-1 py-3 pl-9 pr-4 text-body2 text-muted-foreground">
                {report.quality.reasons.map((reason, index) => (
                  <li key={`${reason}-${index}`}>{reason}</li>
                ))}
              </ul>
            )}
            {!report.acceptedExport && report.artifactReason && (
              <p className="px-4 py-3 text-body2 text-destructive-text">{report.artifactReason}</p>
            )}
          </div>
        </ReportSection>

        <ReportSection title="分析">
          {report.sections.length > 0 ? (
            <div className="space-y-6">
              {report.sections.map(section => (
                <section key={section.id} aria-labelledby={`report-section-${section.id}`}>
                  <h3 id={`report-section-${section.id}`} className="text-body1 font-semibold">{section.title}</h3>
                  {section.paragraphs.map((paragraph, index) => (
                    <p key={`${section.id}-paragraph-${index}`} className="mt-2 whitespace-pre-wrap text-body1 leading-7 text-foreground/90">
                      {paragraph}
                    </p>
                  ))}
                  {section.items.length > 0 && (
                    <ul className="mt-2 list-disc space-y-1 pl-5 text-body1 leading-7 text-foreground/90">
                      {section.items.map((item, index) => (
                        <li key={`${section.id}-item-${index}`}>{item}</li>
                      ))}
                    </ul>
                  )}
                </section>
              ))}
            </div>
          ) : (
            <p className="text-body2 text-muted-foreground">本次运行没有返回 ResearchAnalysis。</p>
          )}
          <details className="mt-6 text-body2 text-muted-foreground">
            <summary className="cursor-pointer hover:text-foreground">运行诊断 JSON</summary>
            <pre className="rk-scroll mt-2 overflow-x-auto whitespace-pre-wrap rounded-xl bg-muted p-3 font-mono text-label">{report.diagnosticJson}</pre>
          </details>
        </ReportSection>

        <ReportSection title="引用">
          {report.citations.length ? (
            <ol className="divide-y rounded-xl border">
              {report.citations.map(citation => (
                <li key={citation.id} className="px-4 py-3 text-body2">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-medium">{citation.label}</span>
                    {citation.valid && (
                      <Link
                        className="text-link underline-offset-4 hover:underline focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
                        href={citationLink(citation)}
                      >
                        回到论文定位
                      </Link>
                    )}
                  </div>
                  {citation.excerpt && (
                    <blockquote className="mt-2 border-l-2 border-border pl-3 text-muted-foreground">
                      {citation.excerpt}
                    </blockquote>
                  )}
                  {!citation.valid && (
                    <p className="mt-2 text-label text-destructive-text">
                      引用未验证：{citation.reasonCode || "来源产物未通过验证"}
                    </p>
                  )}
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-body2 text-muted-foreground">该报告没有可验证的引用定位。</p>
          )}
        </ReportSection>
      </div>
    </main>
  )
}

function ReportSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3">
      <h2 className="text-heading3 font-semibold">{title}</h2>
      {children}
    </section>
  )
}

function PaneStatus({ children }: { children: React.ReactNode }) {
  return <p role="status" className="px-4 py-10 text-center text-body2 text-muted-foreground md:px-6">{children}</p>
}

function ReportDownloads({ report }: { report: ResearchReportView }) {
  const active = useRef<AbortController | null>(null)
  const [busy, setBusy] = useState<"markdown" | "json" | "">("")
  const [message, setMessage] = useState("")
  const [error, setError] = useState("")

  useEffect(() => () => active.current?.abort(), [])

  async function download(format: "markdown" | "json") {
    if (busy) return
    const controller = new AbortController()
    active.current = controller
    setBusy(format)
    setMessage("")
    setError("")
    try {
      const result = await downloadResearchReport(
        report.runId,
        report.sourceRevision,
        format,
        controller.signal,
      )
      const url = URL.createObjectURL(result.blob)
      const link = document.createElement("a")
      link.href = url
      link.download = result.filename
      link.click()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      setMessage(format === "markdown" ? "Markdown 已下载。" : "JSON 已下载。")
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(cause instanceof Error ? cause.message : "报告导出失败，可重试。")
      }
    } finally {
      if (active.current === controller) {
        active.current = null
        setBusy("")
      }
    }
  }

  return (
    <div className="contents">
      <Button type="button" variant="outline" disabled={Boolean(busy)} onClick={() => void download("markdown")}>
        <Download size={15} strokeWidth={1.8} />
        {busy === "markdown" ? "正在导出…" : "导出 Markdown"}
      </Button>
      <Button type="button" variant="outline" disabled={Boolean(busy)} onClick={() => void download("json")}>
        <Download size={15} strokeWidth={1.8} />
        {busy === "json" ? "正在导出…" : "导出 JSON"}
      </Button>
      {message && <span role="status" className="self-center text-body2 text-success-text">{message}</span>}
      {error && <span role="alert" className="self-center text-body2 text-destructive-text">{error}</span>}
    </div>
  )
}

function reportStatusLabel(report: ResearchReportView): string {
  if (report.artifactStatus === "corrupt") return "报告产物已损坏，当前不可导出"
  if (report.artifactStatus === "unauthorized") return "当前账号无权读取报告产物"
  if (report.artifactStatus === "missing") return "报告产物缺失，当前不可导出"
  if (!report.qualityPassed) return "本次分析未通过质量门"
  return "本次运行尚不满足正式报告条件"
}

function qualityLabel(report: ResearchReportView): string {
  if (report.accepted && report.qualityPassed) return "已接受并通过质量门"
  if (report.qualityPassed) return "质量门通过，接受状态待确认"
  return "未通过质量门"
}

function citationLink(citation: ResearchReportView["citations"][number]) {
  const params = new URLSearchParams({ sourceRevision: citation.sourceRevision })
  if (citation.sectionId) params.set("resumeSection", citation.sectionId)
  if (citation.page) params.set("resumePage", String(citation.page))
  return `/papers/${encodeURIComponent(citation.paperId)}/read?${params}`
}
