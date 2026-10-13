"use client"

import { useEffect, useState } from "react"
import Link from "next/link"
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
    return <p role="status">正在核对报告访问身份…</p>
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

  return (
    <div className="space-y-8">
      <header>
        <p className="text-xs font-medium text-primary">RESEARCH REPORTS</p>
        <h1 className="mt-2 text-2xl font-semibold">已保存的精确运行报告</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          每份正式报告都绑定明确的运行与来源修订。多材料准备稿单独列出，不会自动成为综述。
        </p>
      </header>
      {loading && <p role="status">正在读取报告目录…</p>}
      {error && (
        <div role="alert" className="flex flex-wrap items-center gap-3 text-destructive">
          <span>{error}</span>
          <button
            type="button"
            className="underline"
            onClick={() => setAttempt(value => value + 1)}
          >
            重新读取报告目录
          </button>
        </div>
      )}
      {!loading && !error && reports.length === 0 && (
        <p className="rounded-xl border border-dashed p-6 text-sm text-muted-foreground">
          还没有保存通过质量门的 Research 报告。
        </p>
      )}
      <ul className="grid gap-3">
        {reports.map(report => (
          <li key={report.reportId} className="rounded-xl border bg-card p-4">
            <Link
              className="font-medium text-primary hover:underline"
              href={`/reports/${encodeURIComponent(report.reportId)}`}
            >
              {report.title}
            </Link>
            <p className="mt-2 text-xs text-muted-foreground">
              论文 {report.paperId} · 保存于 {formatSavedAt(report.savedAt)}
            </p>
            <details className="mt-2 text-xs text-muted-foreground">
              <summary className="cursor-pointer">技术信息</summary>
              <p className="mt-1 break-all">运行：{report.runId}</p>
              <p className="break-all">来源修订：{report.sourceRevision}</p>
            </details>
          </li>
        ))}
      </ul>

      <section className="space-y-3">
        <div>
          <h2 className="text-lg font-semibold">未验证的报告准备稿</h2>
          <p className="text-sm text-muted-foreground">
            导出只包含你保存的题目、笔记与材料，不代表 Research 质量门已通过。
          </p>
        </div>
        {(workspace.workspace.reportDrafts ?? []).length > 0 ? (
          <ul className="grid gap-3">
            {workspace.workspace.reportDrafts!.map(draft => (
              <DraftRow key={draft.id} draft={draft} />
            ))}
          </ul>
        ) : (
          <p className="rounded-xl border border-dashed p-5 text-sm text-muted-foreground">
            没有报告准备稿。
          </p>
        )}
      </section>
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
    <li className="rounded-xl border bg-amber-50/40 p-4">
      <strong>{draft.title}</strong>
      <p className="mt-1 text-xs text-amber-900">
        未验证准备稿 · {draft.materials.length} 项材料
      </p>
      <div className="mt-3 flex gap-2">
        <button
          type="button"
          disabled={busy}
          className="rounded border px-3 py-1.5 text-sm"
          onClick={() => void download("markdown")}
        >
          导出准备稿 Markdown
        </button>
        <button
          type="button"
          disabled={busy}
          className="rounded border px-3 py-1.5 text-sm"
          onClick={() => void download("json")}
        >
          导出准备稿 JSON
        </button>
      </div>
      {message && <p role="status" className="mt-2 text-sm text-emerald-700">{message}</p>}
      {error && <p role="alert" className="mt-2 text-sm text-destructive">{error}</p>}
    </li>
  )
}

function formatSavedAt(value: string): string {
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString("zh-CN") : value
}
