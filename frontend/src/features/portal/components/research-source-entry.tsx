"use client"

import { useRef, useState } from "react"
import { FileUp, LoaderCircle, RefreshCw } from "lucide-react"
import { usePortalAccount } from "@/components/auth/portal-account-provider"
import styles from "./research-workspace.module.css"

type ImportRecord = { importId: string; status: "received" | "parsing" | "completed" | "failed"; filename?: string; paperId?: string; error?: { message?: string } | null }

export function ResearchSourceEntry() {
  const account = usePortalAccount()
  const input = useRef<HTMLInputElement>(null)
  const [record, setRecord] = useState<ImportRecord | null>(null)
  const [error, setError] = useState("")
  async function upload(file: File) {
    setError(""); setRecord({ importId: "local", status: "parsing", filename: file.name })
    if (file.type !== "application/pdf" && !file.name.toLocaleLowerCase().endsWith(".pdf")) { setRecord(null); setError("请选择 PDF 文件。"); return }
    const response = await fetch("/api/research/imports", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/pdf", "X-Filename": file.name }, body: file }).catch(() => null)
    const payload = await response?.json().catch(() => null)
    if (!response?.ok || !payload?.success) { setRecord(null); setError(payload?.error?.message || "PDF 解析失败，请稍后重试。"); return }
    setRecord(payload.data as ImportRecord)
  }
  async function retry() {
    if (!record || record.importId === "local") return
    setError(""); setRecord({ ...record, status: "parsing", error: null })
    const response = await fetch(`/api/research/imports/${encodeURIComponent(record.importId)}`, { method: "POST", credentials: "same-origin" }).catch(() => null)
    const payload = await response?.json().catch(() => null)
    if (!response?.ok || !payload?.success) { setError(payload?.error?.message || "重试失败，请稍后再试。"); setRecord({ ...record, status: "failed" }); return }
    setRecord(payload.data as ImportRecord)
  }
  return <div className="mt-4 flex flex-wrap items-center gap-3 text-sm">
    <input ref={input} type="file" accept="application/pdf,.pdf" className="sr-only" onChange={event => { const file = event.target.files?.[0]; if (file) void upload(file); event.target.value = "" }} />
    <button type="button" className={styles.button} onClick={event => { if (!account?.session) { account?.openLogin(event.currentTarget); return } input.current?.click() }}><FileUp size={16} />上传 PDF</button>
    <span className="text-[#857191]">PDF 会转换成可检索的章节文本</span>
    {record && <span role="status" className="inline-flex items-center gap-2 text-[#6d28d9]">{record.status === "parsing" ? <><LoaderCircle size={15} className="animate-spin motion-reduce:animate-none" />正在转换 {record.filename}</> : record.status === "completed" ? record.paperId ? <a className="underline" href={`/design-demo/papers/${encodeURIComponent(record.paperId)}/read?returnTo=%2Fdesign-demo`}>打开阅读器</a> : "转换完成，可在论文页查找" : record.status === "failed" ? <><span>{record.error?.message || "转换失败"}</span><button type="button" className="inline-flex items-center gap-1 underline" onClick={() => void retry()}><RefreshCw size={14} />重试</button></> : "已收到 PDF"}</span>}
    {error && <span role="alert" className="text-[#a02b47]">{error}</span>}
  </div>
}
