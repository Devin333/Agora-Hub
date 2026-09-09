"use client"

import { useEffect, useRef, useState } from "react"
import { LoaderCircle, RefreshCw } from "lucide-react"
import { usePortalAccount } from "@/components/auth/portal-account-provider"
import { historyState, saveResearchMaterial } from "@/lib/research/history"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"

type ImportRecord = { importId: string; status: "received" | "parsing" | "completed" | "failed"; filename?: string; paperId?: string; error?: { message?: string } | null }
function validRecord(value: unknown): value is ImportRecord {
  if (!value || typeof value !== "object") return false
  const record = value as ImportRecord
  return /^imp_[a-zA-Z0-9_-]{1,128}$/.test(record.importId) && ["received", "parsing", "completed", "failed"].includes(record.status) && (!record.paperId || /^[a-zA-Z0-9_-]{1,200}$/.test(record.paperId))
}

export function ResearchSourceEntry({ onAttach }: { onAttach?: (id: string) => void }) {
  const workspace = useOwnedResearchWorkspace()
  // Keying the worker prevents previous-account upload state from ever rendering for a new owner.
  return workspace.ready ? <OwnedSourceEntry key={workspace.owner ?? "guest"} owner={workspace.owner!} onAttach={onAttach} /> : null
}

function OwnedSourceEntry({ owner, onAttach }: { owner: string | null; onAttach?: (id: string) => void }) {
  const account = usePortalAccount(), input = useRef<HTMLInputElement>(null)
  const current = useRef(true), request = useRef<AbortController | null>(null), attach = useRef(onAttach)
  attach.current = onAttach
  const key = owner ? `agora-pdf-import:${owner}` : null
  const [record, setRecord] = useState<ImportRecord | null>(null)
  const [filename, setFilename] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [attached, setAttached] = useState(false)
  const isCurrent = () => current.current && historyState().owner === owner
  function keep(record: ImportRecord) {
    if (!isCurrent()) return
    setRecord(record)
    if (key) { try { sessionStorage.setItem(key, JSON.stringify(record)) } catch { /* Server import remains available by identity. */ } }
  }
  useEffect(() => {
    current.current = true
    if (key) { try { const saved = JSON.parse(sessionStorage.getItem(key) ?? "null"); if (validRecord(saved)) { setRecord(saved); setFilename(saved.filename ?? "PDF") } } catch { /* Malformed recovery data is ignored. */ } }
    const open = () => {
      if (request.current) return
      if (!owner) { const trigger = document.querySelector<HTMLElement>('[aria-label="添加到本次研究"]'); if (trigger) account?.openLogin(trigger) }
      else input.current?.click()
    }
    window.addEventListener("agora-open-pdf-import", open)
    return () => { current.current = false; request.current?.abort(); window.removeEventListener("agora-open-pdf-import", open) }
    // Worker owner is immutable; login UI itself is supplied by the same account provider.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [owner])

  async function run(url: string, init: RequestInit = {}): Promise<ImportRecord | undefined> {
    if (!owner || request.current) return
    const abort = new AbortController(); request.current = abort; setBusy(true); setError("")
    const timeout = setTimeout(() => abort.abort("timeout"), 125000)
    try {
      const response = await fetch(url, { ...init, credentials: "same-origin", signal: abort.signal })
      const payload = await response.json()
      if (!isCurrent()) return
      if (!response.ok || !payload.success || !validRecord(payload.data)) throw new Error(payload?.error?.message || "暂时无法读取 PDF 处理结果。")
      keep(payload.data)
      return payload.data
    } catch (cause) {
      if (isCurrent()) setError(abort.signal.reason === "timeout" ? "等待时间较长，处理可能仍在继续。已有导入记录可检查进度。" : cause instanceof Error ? cause.message : "暂时无法处理 PDF，请重试。")
    } finally { clearTimeout(timeout); if (isCurrent()) { setBusy(false); request.current = null } }
  }
  async function upload(file: File) {
    if (busy) return
    setError(""); setAttached(false)
    if (!file.name.toLowerCase().endsWith(".pdf")) { setError("请选择 PDF 文件。"); return }
    if (file.size > 50 * 1024 * 1024) { setError("PDF 不能超过 50 MB，请选择更小的文件。"); return }
    setRecord(null); setFilename(file.name)
    if (key) { try { sessionStorage.removeItem(key) } catch { /* The received identity will remain in memory. */ } }
    const received = await run("/api/research/imports", { method: "POST", headers: { "Content-Type": "application/pdf", "X-Filename": encodeURIComponent(file.name) }, body: file })
    if (received?.status === "received" && isCurrent()) await run(`/api/research/imports/${received.importId}`, { method: "POST" })
  }
  function addToResearch() {
    if (!isCurrent() || !record?.paperId || !attach.current) return
    const now = Date.now(), id = record.importId
    if (saveResearchMaterial({ id, groupId: null, kind: "pdf", title: record.filename || filename || "导入的 PDF", url: "", referenceId: record.paperId, notes: "", createdAt: now, updatedAt: now }, owner)) { attach.current(id); setAttached(true) }
    else setError(historyState().message || "暂时无法引用 PDF，请重试。")
  }
  return <div className={record || error || busy ? "mt-4 flex flex-wrap items-center gap-3 text-sm" : "sr-only"}>
    <input ref={input} type="file" aria-label="上传研究 PDF" accept="application/pdf,.pdf" className="sr-only" onChange={event => { const file = event.target.files?.[0]; if (file) upload(file); event.target.value = "" }} />
    {busy && <span role="status" className="inline-flex items-center gap-2 text-[#6d28d9]"><LoaderCircle size={15} className="animate-spin motion-reduce:animate-none" />正在处理 {filename || record?.filename || "PDF"}…</span>}
    {!busy && record && <span role="status" className="inline-flex flex-wrap items-center gap-3 text-[#6d28d9]">
      <span>{record.filename || filename}</span>
      {record.status === "completed" ? record.paperId ? <><span>文本已转换，可用于本次研究</span>{onAttach && <button type="button" disabled={attached} className="underline" onClick={addToResearch}>{attached ? "已加入本次研究" : "用于本次研究"}</button>}</> : <span>文件处理已结束，但未取得可用正文，请重新检查。</span> : <><span>{record.status === "failed" ? record.error?.message || "转换失败，可以重试" : record.status === "received" ? "已上传，等待转换" : "转换尚未确认完成"}</span><button type="button" className="inline-flex items-center gap-1 underline" onClick={() => void run(`/api/research/imports/${record.importId}`, { method: "GET" })}>检查进度</button><button type="button" className="inline-flex items-center gap-1 underline" onClick={() => void run(`/api/research/imports/${record.importId}`, { method: "POST" })}><RefreshCw size={14} />{record.status === "failed" ? "重试" : "继续转换"}</button></>}
    </span>}
    {error && <span role="alert" className="text-[#a02b47]">{error}</span>}
  </div>
}
