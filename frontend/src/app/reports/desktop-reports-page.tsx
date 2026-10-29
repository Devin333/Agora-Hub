"use client"

import { useEffect, useState } from "react"
import Link from "next/link"
import { Download } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  exportResearchDraft,
  fetchSavedResearchReports,
} from "@/lib/research/desktop/client"
import type { ResearchReportReference } from "@/lib/research/desktop/contracts"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import type { ResearchReportDraft } from "@/lib/research/workspace-items"

const MAX_DRAFT_EXPORT_BYTES = 20 * 1024 * 1024

export function DesktopReportsPage() {
  const workspace = useOwnedResearchWorkspace()
  if (!workspace.ready || !workspace.owner) {
    return <p role="status" className="px-4 py-10 text-center text-body2 text-muted-foreground md:px-6">正在核对报告访问身份…</p>
  }
  return <OwnedReportsPage key={workspace.owner} owner={workspace.owner} />
}

function OwnedReportsPage({ owner }: { owner: string }) {
  const workspace = useOwnedResearchWorkspace()
  const [reports, setReports] = useState<ResearchReportReference[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError("")
    fetchSavedResearchReports(controller.signal)
      .then(value => {
        if (workspace.owner === owner) setReports(value)
      })
      .catch(cause => {
        if (!controller.signal.aborted && workspace.owner === owner) {
          setError(cause instanceof Error ? cause.message : "报告目录暂时不可用。")
        }
      })
      .finally(() => {
        if (!controller.signal.aborted && workspace.owner === owner) setLoading(false)
      })
    return () => controller.abort()
  }, [attempt, owner, workspace.owner])

  const drafts = workspace.workspace.reportDrafts ?? []

  return (
    <div className="flex min-h-full flex-col">
      <header className="border-b px-4 py-4 md:px-6">
        <h1 className="text-heading2 font-semibold">已保存的精确运行报告</h1>
        <p className="mt-1 max-w-3xl text-body2 text-muted-foreground">
          每份正式报告都绑定明确的运行与来源修订。多材料准备稿单独列出，不会自动成为综述。
        </p>
      </header>
      <div className="mx-auto w-full max-w-[60rem] space-y-8 px-4 py-6 md:px-6">
        <section aria-label="正式报告" className="space-y-3">
          {loading && <p role="status" className="text-body2 text-muted-foreground">正在读取报告目录…</p>}
          {error && (
            <div role="alert" className="flex flex-wrap items-center gap-3 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-body2 text-destructive-text">
              <span>{error}</span>
              <Button type="button" variant="outline" size="sm" onClick={() => setAttempt(value => value + 1)}>
                重新读取报告目录
              </Button>
            </div>
          )}
          {!loading && !error && reports.length === 0 && (
            <p className="py-10 text-center text-body2 text-muted-foreground">
              还没有保存通过质量门的 Research 报告。
            </p>
          )}
          {reports.length > 0 && (
            <ul className="divide-y rounded-xl border">
              {reports.map(report => (
                <li key={report.reportId} className="px-4 py-3">
                  <Link
                    className="text-body1 font-medium text-foreground underline-offset-4 hover:underline focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
                    href={`/reports/${encodeURIComponent(report.reportId)}`}
                  >
                    {report.title}
                  </Link>
                  <p className="mt-1 text-label text-muted-foreground">
                    论文 {report.paperId} · 保存于 {formatSavedAt(report.savedAt)}
                  </p>
                  <details className="mt-1 text-label text-faint-foreground">
                    <summary className="cursor-pointer hover:text-muted-foreground">技术信息</summary>
                    <p className="mt-1 break-all">运行：{report.runId}</p>
                    <p className="break-all">来源修订：{report.sourceRevision}</p>
                  </details>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="space-y-3">
          <div>
            <h2 className="text-heading3 font-semibold">未验证的报告准备稿</h2>
            <p className="mt-1 text-body2 text-muted-foreground">
              导出只包含你保存的题目、笔记与材料，不代表 Research 质量门已通过。
            </p>
          </div>
          {drafts.length > 0 ? (
            <ul className="divide-y rounded-xl border">
              {drafts.map(draft => (
                <DraftRow key={draft.id} draft={draft} />
              ))}
            </ul>
          ) : (
            <p className="py-6 text-body2 text-muted-foreground">没有报告准备稿。</p>
          )}
        </section>
      </div>
    </div>
  )
}

function DraftRow({ draft }: { draft: ResearchReportDraft }) {
  const [error, setError] = useState("")
  const [message, setMessage] = useState("")
  const [busy, setBusy] = useState(false)

  async function download(format: "markdown" | "json") {
    setBusy(true)
    setError("")
    setMessage("")
    try {
      const response = await exportResearchDraft({
        title: draft.title,
        question: draft.question,
        notes: draft.notes,
        materials: draft.materials,
        format,
      })
      if (!response.ok) {
        const payload = await response.json().catch(() => null)
        throw new Error(payload?.error?.message || "准备稿导出失败。")
      }
      const declared = Number(response.headers.get("content-length") ?? "0")
      if (Number.isFinite(declared) && declared > MAX_DRAFT_EXPORT_BYTES) {
        throw new Error("准备稿导出内容超过大小限制。")
      }
      const blob = await response.blob()
      if (blob.size > MAX_DRAFT_EXPORT_BYTES) {
        throw new Error("准备稿导出内容超过大小限制。")
      }
      const url = URL.createObjectURL(blob)
      const link = document.createElement("a")
      link.href = url
      link.download = `${draft.id}.${format === "markdown" ? "md" : "json"}`
      link.click()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      setMessage("准备稿已下载，内容仍未通过 Research 质量门。")
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "准备稿导出失败。")
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className="flex flex-wrap items-start justify-between gap-3 px-4 py-3">
      <div className="min-w-0 flex-1">
        <p className="text-body1 font-medium text-foreground">{draft.title}</p>
        <p className="mt-1 flex items-center gap-2 text-label text-muted-foreground">
          <Badge variant="warning">未验证准备稿</Badge>
          {draft.materials.length} 项材料
        </p>
        {message && <p role="status" className="mt-2 text-body2 text-success-text">{message}</p>}
        {error && <p role="alert" className="mt-2 text-body2 text-destructive-text">{error}</p>}
      </div>
      <div className="flex shrink-0 gap-2">
        <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void download("markdown")}>
          <Download size={15} strokeWidth={1.8} />
          导出准备稿 Markdown
        </Button>
        <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void download("json")}>
          <Download size={15} strokeWidth={1.8} />
          导出准备稿 JSON
        </Button>
      </div>
    </li>
  )
}

function formatSavedAt(value: string): string {
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString("zh-CN") : value
}
