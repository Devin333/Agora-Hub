"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { Check, Download, LoaderCircle, Plus, RefreshCw, ShieldCheck, Trash2, X } from "lucide-react"
import {
  confirmPersonalMemory,
  createPersonalMemory,
  deletePersonalMemory,
  exportPersonalMemory,
  getPersonalMemoryInfo,
  getPersonalMemoryPolicy,
  listPersonalMemoryPage,
  rebuildPersonalMemoryIndex,
  rejectPersonalMemoryCandidate,
  setPersonalMemoryStatus,
  updatePersonalMemory,
  updatePersonalMemoryPolicy,
  type PersonalMemoryCategory,
  type PersonalMemoryCreation,
  type PersonalMemoryInfo,
  type PersonalMemoryPolicy,
  type PersonalMemoryRecord,
  type PersonalMemorySourceRef,
} from "./personal-memory-client"
import { saveUserExport } from "./user-export"
import styles from "./paper-agent-app.module.css"

const categoryLabels: Record<PersonalMemoryCategory, string> = {
  research_interest: "研究兴趣",
  reading_preference: "阅读偏好",
  background: "背景信息",
}

type MemorySourceContext = {
  sourceRef: string
  label: string
  sourceRevision?: string
}

type MemoryEditDraft = {
  memoryId: string
  revision: number
  content: string
  category: PersonalMemoryCategory
  originalContent: string
  originalCategory: PersonalMemoryCategory
}


function matchesMemoryView(record: PersonalMemoryRecord, includeDeleted: boolean, statusFilter: "all" | PersonalMemoryRecord["status"]): boolean {
  if (!includeDeleted && record.status === "deleted") return false
  return statusFilter === "all" || record.status === statusFilter
}

export function PersonalMemoryPanel({
  open,
  namespace,
  sourceContext,
  onClose,
  onAuthorizedChange,
  onPolicyChange,
  onDraftChange,
  onActivityChange,
}: {
  open: boolean
  namespace?: string
  sourceContext?: MemorySourceContext | null
  onClose: () => void
  onAuthorizedChange: (records: PersonalMemoryRecord[]) => void
  onPolicyChange: (policy: PersonalMemoryPolicy | null) => void
  onDraftChange: (dirty: boolean) => void
  onActivityChange: (active: boolean) => void
}) {
  const panelRef = useRef<HTMLElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const contentRef = useRef<HTMLTextAreaElement>(null)
  const editContentRef = useRef<HTMLTextAreaElement>(null)
  const createRequestRef = useRef("")
  const createPayloadRef = useRef("")
  const [records, setRecords] = useState<PersonalMemoryRecord[]>([])
  const [policy, setPolicy] = useState<PersonalMemoryPolicy | null>(null)
  const [info, setInfo] = useState<PersonalMemoryInfo | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const [content, setContent] = useState("")
  const [category, setCategory] = useState<PersonalMemoryCategory>("research_interest")
  const [editDraft, setEditDraft] = useState<MemoryEditDraft | null>(null)
  const [busyId, setBusyId] = useState("")
  const [includeDeleted, setIncludeDeleted] = useState(false)
  const [statusFilter, setStatusFilter] = useState<"all" | PersonalMemoryRecord["status"]>("all")
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [total, setTotal] = useState(0)
  const recordsRef = useRef<PersonalMemoryRecord[]>([])
  const nextCursorRef = useRef<string | null>(null)

  useMemoryDialogFocus(open, panelRef, closeRef)

  const publishRecords = useCallback((next: PersonalMemoryRecord[]) => {
    recordsRef.current = next
    setRecords(next)
    onAuthorizedChange(next.filter(item => item.status === "active"))
  }, [onAuthorizedChange])

  const load = useCallback(async (
    reset = true,
    includeDeletedValue = false,
    statusFilterValue: "all" | PersonalMemoryRecord["status"] = "all",
  ) => {
    setLoading(true); setError(""); setNotice("")
    const statuses = statusFilterValue !== "all"
      ? [statusFilterValue]
      : includeDeletedValue ? ["candidate", "active", "disabled", "deleted"] as const : undefined
    if (!reset) {
      try {
        const page = await listPersonalMemoryPage({ cursor: nextCursorRef.current ?? undefined, limit: 50, statuses: statuses ? [...statuses] : undefined })
        publishRecords([...recordsRef.current, ...page.records.filter(record => !recordsRef.current.some(existing => existing.memory_id === record.memory_id))])
        nextCursorRef.current = page.nextCursor; setNextCursor(page.nextCursor); setTotal(page.total)
      } catch (cause) { setError(cause instanceof Error ? cause.message : "个人记忆下一页读取失败。") }
      finally { setLoading(false) }
      return
    }
    const results = await Promise.allSettled([listPersonalMemoryPage({ limit: 50, statuses: statuses ? [...statuses] : undefined }), getPersonalMemoryPolicy(), getPersonalMemoryInfo()])
    const [recordsResult, policyResult, infoResult] = results
    if (recordsResult.status === "fulfilled") {
      publishRecords(recordsResult.value.records)
      nextCursorRef.current = recordsResult.value.nextCursor
      setNextCursor(recordsResult.value.nextCursor)
      setTotal(recordsResult.value.total)
    }
    if (policyResult.status === "fulfilled") { setPolicy(policyResult.value); onPolicyChange(policyResult.value) }
    if (infoResult.status === "fulfilled") setInfo(infoResult.value)
    const failure = results.find(result => result.status === "rejected")
    if (failure?.status === "rejected") setError(failure.reason instanceof Error ? failure.reason.message : "个人记忆服务暂时不可用。")
    setLoading(false)
  }, [onPolicyChange, publishRecords])
  useEffect(() => {
    if (!open) return
    void load(true, includeDeleted, statusFilter)
  }, [includeDeleted, load, open, statusFilter])
  const editDirty = Boolean(editDraft && (
    editDraft.content.trim() !== editDraft.originalContent
    || editDraft.category !== editDraft.originalCategory
  ))
  useEffect(() => { onDraftChange(Boolean(content.trim()) || editDirty) }, [content, editDirty, onDraftChange])
  useEffect(() => { onActivityChange(Boolean(busyId) || loading) }, [busyId, loading, onActivityChange])
  const requestClose = useCallback(() => {
    if (loading || busyId) {
      setNotice("操作正在进行；个人记忆面板保持打开，完成后可关闭。")
      return false
    }
    if ((content.trim() || editDirty) && !window.confirm("尚有未保存的个人记忆输入。放弃并关闭吗？")) {
      if (editDirty) editContentRef.current?.focus({ preventScroll: true })
      else contentRef.current?.focus({ preventScroll: true })
      return false
    }
    if (content || editDraft) {
      setContent("")
      setEditDraft(null)
      createRequestRef.current = ""
      createPayloadRef.current = ""
      onDraftChange(false)
    }
    onClose()
    return true
  }, [busyId, content, editDraft, editDirty, loading, onClose, onDraftChange])

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return
      event.preventDefault()
      event.stopImmediatePropagation()
      requestClose()
    }
    // Window capture runs before the App document shortcut listener, so one Escape
    // cannot close both this dialog and another underlying surface.
    window.addEventListener("keydown", onKeyDown, true)
    return () => window.removeEventListener("keydown", onKeyDown, true)
  }, [open, requestClose])

  async function createRecord(creation: PersonalMemoryCreation) {
    const value = content.trim(); if (!value || busyId || loading) return
    const payloadIdentity = JSON.stringify([value, category, creation, sourceContext ?? null])
    const requestId = createRequestRef.current && createPayloadRef.current === payloadIdentity
      ? createRequestRef.current
      : `paper-agent/${crypto.randomUUID()}`
    createRequestRef.current = requestId
    createPayloadRef.current = payloadIdentity
    const source_refs: PersonalMemorySourceRef[] = sourceContext ? [{
      source_type: "paper",
      source_ref: sourceContext.sourceRef,
      ...(sourceContext.sourceRevision ? { source_revision: sourceContext.sourceRevision } : {}),
      label: sourceContext.label,
    }] : [{
      source_type: "user_input",
      source_ref: `paper-agent:${requestId}`,
      label: "论文 App 手动输入",
    }]
    setBusyId(`new-${creation}`); setError("")
    try {
      const record = await createPersonalMemory({ content: value, category, source_refs, creation, request_id: requestId })
      const existed = recordsRef.current.some(item => item.memory_id === record.memory_id)
      if (matchesMemoryView(record, includeDeleted, statusFilter)) {
        publishRecords([record, ...recordsRef.current.filter(item => item.memory_id !== record.memory_id)])
        if (!existed) setTotal(current => current + 1)
      }
      setContent(""); createRequestRef.current = ""; createPayloadRef.current = ""
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "记忆保存失败；输入和 request id 已保留，可安全重试。")
    } finally { setBusyId("") }
  }

  async function replaceRecord(record: PersonalMemoryRecord, action: () => Promise<PersonalMemoryRecord>): Promise<boolean> {
    if (busyId || loading) return false
    setBusyId(record.memory_id); setError("")
    try {
      const updated = await action()
      const before = recordsRef.current
      const wasVisible = before.some(item => item.memory_id === updated.memory_id)
      const staysVisible = matchesMemoryView(updated, includeDeleted, statusFilter)
      publishRecords(before
        .map(item => item.memory_id === updated.memory_id ? updated : item)
        .filter(item => matchesMemoryView(item, includeDeleted, statusFilter)))
      if (wasVisible && !staysVisible) setTotal(current => Math.max(0, current - 1))
      return true
    }
    catch (cause) {
      setError(cause instanceof Error ? cause.message : "记忆更新失败；编辑输入仍保留。")
      return false
    }
    finally { setBusyId("") }
  }

  function beginEdit(record: PersonalMemoryRecord) {
    if (busyId || loading) return
    if (editDirty && !window.confirm("放弃当前未保存的记忆修改，编辑另一条吗？")) return
    setEditDraft({
      memoryId: record.memory_id,
      revision: record.revision,
      content: record.content,
      category: record.category,
      originalContent: record.content,
      originalCategory: record.category,
    })
    setError("")
  }

  async function saveEdit(record: PersonalMemoryRecord) {
    if (!editDraft || editDraft.memoryId !== record.memory_id || busyId || loading) return
    if (editDraft.revision !== record.revision) {
      setError("服务端版本已改变；编辑输入仍保留。请先核对最新版本，再明确选择基于最新版本重试。")
      return
    }
    const nextContent = editDraft.content.trim()
    if (!nextContent) {
      setError("个人记忆内容不能为空；编辑输入仍保留。")
      editContentRef.current?.focus({ preventScroll: true })
      return
    }
    const patch: Partial<Pick<PersonalMemoryRecord, "content" | "category">> = {}
    if (nextContent !== editDraft.originalContent) patch.content = nextContent
    if (editDraft.category !== editDraft.originalCategory) patch.category = editDraft.category
    if (Object.keys(patch).length === 0) {
      setEditDraft(null)
      return
    }
    const saved = await replaceRecord(record, () => updatePersonalMemory(
      record.memory_id,
      editDraft.revision,
      patch,
    ))
    if (saved) setEditDraft(null)
  }

  function cancelEdit() {
    if (busyId || loading) return
    if (editDirty && !window.confirm("放弃未保存的记忆修改吗？")) return
    setEditDraft(null)
  }

  function rebaseEditOnLatestRevision(record: PersonalMemoryRecord) {
    if (!editDraft || busyId || loading) return
    if (!window.confirm(`当前服务端版本 r${record.revision}。内容：${record.content}。以保留的输入覆盖此版本吗？`)) return
    setEditDraft({
      ...editDraft,
      revision: record.revision,
      originalContent: record.content,
      originalCategory: record.category,
    })
    setError("")
  }

  async function rejectCandidate(record: PersonalMemoryRecord) {
    if (!window.confirm("拒绝后该候选不会进入召回，并将保留删除 tombstone。继续吗？")) return
    await replaceRecord(record, () => rejectPersonalMemoryCandidate(record.memory_id, record.revision))
  }

  async function removeRecord(record: PersonalMemoryRecord) {
    if (!window.confirm("删除后将按服务端策略保留审计身份。继续吗？")) return
    await replaceRecord(record, () => deletePersonalMemory(record.memory_id, record.revision))
  }

  async function setRemotePolicy(allow: boolean) {
    if (!policy || busyId || loading) return
    setBusyId("policy"); setError("")
    try { const updated = await updatePersonalMemoryPolicy(policy.revision, allow); setPolicy(updated); onPolicyChange(updated) }
    catch (cause) { setError(cause instanceof Error ? cause.message : "联网策略保存失败。") }
    finally { setBusyId("") }
  }

  async function rebuildIndex() {
    if (busyId || loading || editDirty) return
    setBusyId("rebuild-index"); setError(""); setNotice("")
    try {
      const result = await rebuildPersonalMemoryIndex()
      setNotice(`召回索引已按当前 active revision 重建：${result.indexedRecords} 条。`)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "个人记忆索引重建失败；现有记录未被改写。")
    } finally { setBusyId("") }
  }

  async function exportRecords() {
    if (busyId || loading) return
    setBusyId("export"); setError(""); setNotice("")
    try {
      const value = await exportPersonalMemory(includeDeleted)
      const result = await saveUserExport({ suggestedName: value.filename.replace(/\.json$/i, ""), format: "json", content: await value.blob.text() })
      if (result.ok) setNotice(result.message)
      else setError(result.code === "cancelled" ? "已取消个人记忆导出。" : result.message)
    } catch (cause) { setError(cause instanceof Error ? cause.message : "个人记忆导出失败。") }
    finally { setBusyId("") }
  }

  if (!open) return null
  const visibleRecords = includeDeleted ? records : records.filter(record => record.status !== "deleted")
  const storagePath = info?.database_path ?? info?.data_root ?? "由本机服务决定"

  const remotePermissions = policy?.remote_processing_permissions ?? {
    model: policy?.allow_remote_processing ?? false,
    tool: policy?.allow_remote_processing ?? false,
    embedding: policy?.allow_remote_processing ?? false,
  }

  return <div className={styles.memoryOverlay} role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) requestClose() }}>
    <aside ref={panelRef} className={styles.memoryPanel} role="dialog" aria-modal="true" aria-labelledby="memory-title" tabIndex={-1}>
      <header><div><span className={styles.eyebrow}>LOCAL MEMORY</span><h2 id="memory-title">个人记忆</h2></div><button ref={closeRef} type="button" aria-label="关闭个人记忆" onClick={requestClose}><X size={18} /></button></header>
      <section className={styles.memoryInfo}>
        <p><strong>命名空间</strong><span>{namespace ?? "正在读取"}</span></p>
        <p><strong>本机路径</strong><span title={storagePath}>{storagePath}</span></p>
        <p><strong>联网规则</strong><span>{policy?.allow_remote_processing ? "策略允许模型、工具和 embedding；本次不用记忆仍优先" : "模型、工具和 embedding 默认全部禁止"}</span></p>
      </section>
      <section className={styles.policyCard}>
        <div><ShieldCheck size={17} /><div><strong>允许远程处理个人记忆</strong><p>这是持久化 policy，不是本页临时开关；模型、工具和 embedding 三路统一受控，并使用 revision CAS。</p><small>模型 {remotePermissions.model ? "允许" : "禁止"} · 工具 {remotePermissions.tool ? "允许" : "禁止"} · embedding {remotePermissions.embedding ? "允许" : "禁止"}</small></div></div>
        <button type="button" disabled={!policy || Boolean(busyId) || loading} aria-pressed={policy?.allow_remote_processing ?? false} onClick={() => void setRemotePolicy(!policy?.allow_remote_processing)}>{busyId === "policy" ? "保存中" : policy?.allow_remote_processing ? "已允许" : "未允许"}</button>
      </section>
      <section className={styles.memoryCreate}><textarea ref={contentRef} disabled={Boolean(busyId) || loading} maxLength={8000} value={content} onChange={event => { setContent(event.target.value); if (!event.target.value.trim()) createRequestRef.current = "" }} placeholder="写入你明确希望保留的研究偏好或背景…" aria-label="新个人记忆" /><div><select aria-label="记忆类别" disabled={Boolean(busyId) || loading} value={category} onChange={event => setCategory(event.target.value as PersonalMemoryCategory)}>{Object.entries(categoryLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><button type="button" disabled={!content.trim() || Boolean(busyId) || loading} onClick={() => void createRecord("candidate")}><Plus size={14} />保存候选</button><button type="button" disabled={!content.trim() || Boolean(busyId) || loading} onClick={() => void createRecord("confirmed")}><Check size={14} />保存并确认</button></div><small>来源：{sourceContext ? `当前论文「${sourceContext.label}」` : "本页手动用户输入"}。确认写入会直接成为 active；候选需后续确认或拒绝。</small></section>
      <div className={styles.memoryToolbar}><button type="button" onClick={() => void load(true, includeDeleted, statusFilter)} disabled={loading || Boolean(busyId)}><RefreshCw className={loading ? styles.spinner : ""} size={14} />刷新</button><label>状态<select aria-label="记忆状态筛选" disabled={loading || Boolean(busyId) || editDirty} value={statusFilter} onChange={event => setStatusFilter(event.target.value as "all" | PersonalMemoryRecord["status"])}><option value="all">全部当前记录</option><option value="candidate">候选</option><option value="active">已启用</option><option value="disabled">已停用</option>{includeDeleted && <option value="deleted">已删除</option>}</select></label><label><input type="checkbox" disabled={loading || Boolean(busyId) || editDirty} checked={includeDeleted} onChange={event => { setIncludeDeleted(event.target.checked); if (!event.target.checked && statusFilter === "deleted") setStatusFilter("all") }} />包含已删除历史</label><button type="button" onClick={() => void rebuildIndex()} disabled={Boolean(busyId) || loading || editDirty}><RefreshCw size={14} />重建召回索引</button><button type="button" onClick={() => void exportRecords()} disabled={Boolean(busyId) || loading}><Download size={14} />导出 JSON</button></div>
      <p className={styles.memoryPageStatus}>已加载 {records.length} / {total} 条{nextCursor ? <button type="button" disabled={loading || Boolean(busyId)} onClick={() => void load(false, includeDeleted, statusFilter)}>加载更多</button> : null}</p>
      {error && <p className={styles.errorText} role="alert">{error}</p>}
      {notice && <p className={styles.memoryLoading} role="status">{notice}</p>}
      {loading && records.length === 0 ? <p role="status" className={styles.memoryLoading}><LoaderCircle className={styles.spinner} size={16} />读取真实记忆记录…</p> : null}
      {!loading && visibleRecords.length === 0 ? <p className={styles.memoryEmpty}>没有符合当前视图的真实记忆记录。</p> : null}
      <div className={styles.memoryRecords}>{visibleRecords.map(record => {
        const activeEdit = editDraft?.memoryId === record.memory_id ? editDraft : null
        return <article key={record.memory_id} data-status={record.status}>
          <div><span>{categoryLabels[record.category]}</span><code>r{record.revision}</code></div>
          {activeEdit ? <section className={styles.memoryCreate}>
            <textarea ref={editContentRef} disabled={Boolean(busyId)} aria-label="编辑记忆内容" maxLength={8000} value={activeEdit.content} onChange={event => setEditDraft(current => current && current.memoryId === record.memory_id ? { ...current, content: event.target.value } : current)} />
            <div><select aria-label="编辑记忆类别" disabled={Boolean(busyId)} value={activeEdit.category} onChange={event => setEditDraft(current => current && current.memoryId === record.memory_id ? { ...current, category: event.target.value as PersonalMemoryCategory } : current)}>{Object.entries(categoryLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><button type="button" disabled={Boolean(busyId) || loading || !activeEdit.content.trim() || activeEdit.revision !== record.revision} onClick={() => void saveEdit(record)}>保存修改</button><button type="button" disabled={Boolean(busyId) || loading} onClick={cancelEdit}>取消</button>{activeEdit.revision !== record.revision && <button type="button" disabled={Boolean(busyId) || loading} onClick={() => rebaseEditOnLatestRevision(record)}>基于最新版本重试</button>}</div>
          </section> : <p>{record.content}</p>}
          <details><summary>来源 {record.source_refs.length} 项</summary>{record.source_refs.map((ref, index) => <p key={`${ref.source_type}:${ref.source_ref}:${index}`}><strong>{ref.source_type}</strong> {ref.label ? `${ref.label} · ` : ""}{ref.source_ref}{ref.source_revision ? ` · ${ref.source_revision}` : ""}{ref.locator ? ` · ${ref.locator}` : ""}</p>)}</details>
          {!activeEdit && <div className={styles.recordActions}>
            <button type="button" disabled={Boolean(busyId) || loading || record.status === "deleted"} onClick={() => beginEdit(record)}>编辑</button>
            {record.status === "candidate" && <><button type="button" disabled={Boolean(busyId) || loading} onClick={() => void replaceRecord(record, () => confirmPersonalMemory(record.memory_id, record.revision))}><Check size={14} />确认</button><button type="button" disabled={Boolean(busyId) || loading} onClick={() => void rejectCandidate(record)}><Trash2 size={14} />拒绝</button></>}
            {record.status === "active" && <button type="button" disabled={Boolean(busyId) || loading} onClick={() => void replaceRecord(record, () => setPersonalMemoryStatus(record.memory_id, record.revision, "disabled"))}>停用</button>}
            {record.status === "disabled" && <button type="button" disabled={Boolean(busyId) || loading} onClick={() => void replaceRecord(record, () => setPersonalMemoryStatus(record.memory_id, record.revision, "active"))}>启用</button>}
            {record.status !== "candidate" && <button type="button" disabled={Boolean(busyId) || loading || record.status === "deleted"} onClick={() => void removeRecord(record)}><Trash2 size={14} />删除</button>}
          </div>}
          <small>状态：{record.status} · 更新：{new Date(record.updated_at).toLocaleString("zh-CN")}</small>
        </article>
      })}</div>
    </aside>
  </div>
}

/** Keep the dialog background unavailable until close, then restore the opener. */
function useMemoryDialogFocus(
  open: boolean,
  panelRef: { current: HTMLElement | null },
  closeRef: { current: HTMLButtonElement | null },
) {
  useEffect(() => {
    const panel = panelRef.current
    if (!open || !panel) return
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const background: Array<{ element: HTMLElement; inert: string | null; ariaHidden: string | null }> = []

    for (let branch: HTMLElement = panel; branch.parentElement; branch = branch.parentElement) {
      for (const sibling of Array.from(branch.parentElement.children)) {
        if (sibling === branch || !(sibling instanceof HTMLElement)) continue
        background.push({ element: sibling, inert: sibling.getAttribute("inert"), ariaHidden: sibling.getAttribute("aria-hidden") })
        sibling.setAttribute("inert", "")
        sibling.setAttribute("aria-hidden", "true")
      }
      if (branch.parentElement === document.body) break
    }

    const focusable = () => Array.from(panel.querySelectorAll<HTMLElement>(
      'button, input:not([type="hidden"]), select, textarea, a[href], summary, [tabindex], [contenteditable="true"]',
    )).filter(element => {
      if (element.tabIndex < 0 || element.matches(":disabled") || element.closest('[hidden], [inert], [aria-hidden="true"]')) return false
      for (let ancestor: HTMLElement | null = element; ancestor; ancestor = ancestor.parentElement) {
        const style = window.getComputedStyle(ancestor)
        if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return false
        if (ancestor === panel) break
      }
      return true
    })

    let lastFocused: HTMLElement = closeRef.current ?? panel
    const focusInside = () => {
      const controls = focusable()
      const target = controls.includes(lastFocused) ? lastFocused : controls[0] ?? panel
      target.focus({ preventScroll: true })
    }
    const onFocusIn = (event: FocusEvent) => {
      if (event.target instanceof HTMLElement && panel.contains(event.target)) lastFocused = event.target
      else focusInside()
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || event.altKey || event.ctrlKey || event.metaKey) return
      event.preventDefault()
      const controls = focusable()
      const index = controls.indexOf(document.activeElement as HTMLElement)
      const nextIndex = index < 0
        ? (event.shiftKey ? controls.length - 1 : 0)
        : (index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length
      ;(controls[nextIndex] ?? panel).focus({ preventScroll: true })
    }

    document.addEventListener("focusin", onFocusIn, true)
    document.addEventListener("keydown", onKeyDown, true)
    focusInside()
    return () => {
      document.removeEventListener("focusin", onFocusIn, true)
      document.removeEventListener("keydown", onKeyDown, true)
      for (const { element, inert, ariaHidden } of background) {
        if (inert === null) element.removeAttribute("inert")
        else element.setAttribute("inert", inert)
        if (ariaHidden === null) element.removeAttribute("aria-hidden")
        else element.setAttribute("aria-hidden", ariaHidden)
      }
      if (opener?.isConnected && !opener.closest("[inert]")) opener.focus({ preventScroll: true })
    }
  }, [open, panelRef, closeRef])
}

