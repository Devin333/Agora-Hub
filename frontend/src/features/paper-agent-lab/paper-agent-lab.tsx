"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ArrowLeft, Menu, PanelLeftClose } from "lucide-react"
import { AssistantPanel } from "./assistant-panel"
import { PREVIEW_PAPERS, TOPICS } from "./data"
import { LibraryPanel } from "./library-panel"
import { createPreviewHref, DEFAULT_PREVIEW_STATE, parsePreviewState } from "./preview-state"
import { ReaderPanel } from "./reader-panel"
import type { LibraryView, PaperSort, PreviewQuestion, PreviewState, ReaderTab, TopicId } from "./types"
import { WorkspaceSidebar } from "./workspace-sidebar"
import styles from "./paper-agent-lab.module.css"

export interface PaperAgentLabProps {
  initialState?: PreviewState
}

const VIEW_LABELS: Record<LibraryView, string> = {
  all: "全部论文",
  unread: "未读论文",
  saved: "我的收藏",
}

function isEditableTarget(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (
    target.isContentEditable
    || target.tagName === "INPUT"
    || target.tagName === "TEXTAREA"
    || target.tagName === "SELECT"
  )
}

function trapFocus(event: KeyboardEvent, container: HTMLElement | null) {
  if (event.key !== "Tab" || !container) return
  const focusable = Array.from(container.querySelectorAll<HTMLElement>(
    'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )).filter((element) => !element.hasAttribute("hidden") && element.getClientRects().length > 0)

  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false)

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return
    const media = window.matchMedia(query)
    const update = () => setMatches(media.matches)
    update()
    media.addEventListener("change", update)
    return () => media.removeEventListener("change", update)
  }, [query])

  return matches
}

export function PaperAgentLab({ initialState = DEFAULT_PREVIEW_STATE }: PaperAgentLabProps) {
  const [previewState, setPreviewState] = useState<PreviewState>(initialState)
  const previewStateRef = useRef<PreviewState>(initialState)
  const [query, setQuery] = useState("")
  const [topic, setTopic] = useState<TopicId | null>(null)
  const [sort, setSort] = useState<PaperSort>("default")
  const [savedIds, setSavedIds] = useState<ReadonlySet<string>>(() => new Set())
  const [readIds, setReadIds] = useState<ReadonlySet<string>>(() => new Set())
  const [notes, setNotes] = useState<Readonly<Record<string, string>>>({})
  const [conversation, setConversation] = useState<readonly PreviewQuestion[]>([])
  const [pendingPrompt, setPendingPrompt] = useState<string | undefined>()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [mobileReaderOpen, setMobileReaderOpen] = useState(false)

  const searchInputRef = useRef<HTMLInputElement>(null)
  const sidebarRef = useRef<HTMLElement>(null)
  const sidebarCloseRef = useRef<HTMLButtonElement>(null)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const assistantShellRef = useRef<HTMLDivElement>(null)
  const assistantReturnFocusRef = useRef<HTMLElement | null>(null)
  const questionSequenceRef = useRef(0)
  const isNarrow = useMediaQuery("(max-width: 1100px)")

  const filteredPapers = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase("zh-CN")
    const visible = PREVIEW_PAPERS.filter((paper) => {
      if (previewState.view === "saved" && !savedIds.has(paper.id)) return false
      if (previewState.view === "unread" && readIds.has(paper.id)) return false
      if (topic !== null && paper.topic !== topic) return false
      if (!normalizedQuery) return true
      return [paper.title, paper.titleZh, paper.authors, paper.venue, ...paper.tags]
        .some((value) => value.toLocaleLowerCase("zh-CN").includes(normalizedQuery))
    })

    if (sort === "year-desc") return [...visible].sort((left, right) => right.year - left.year)
    if (sort === "title") return [...visible].sort((left, right) => left.title.localeCompare(right.title, "en"))
    return visible
  }, [previewState.view, query, readIds, savedIds, sort, topic])

  const selectedPaper = filteredPapers.find((paper) => paper.id === previewState.paperId) ?? filteredPapers[0] ?? null

  const listHeading = topic === null
    ? VIEW_LABELS[previewState.view]
    : TOPICS.find((item) => item.id === topic)?.label ?? VIEW_LABELS.all

  const updatePreviewState = useCallback((patch: Partial<PreviewState>, mode: "push" | "replace" = "push") => {
    const current = previewStateRef.current
    const changed = Object.entries(patch).some(([key, value]) => current[key as keyof PreviewState] !== value)
    if (!changed) return

    const next = { ...current, ...patch }
    previewStateRef.current = next
    setPreviewState(next)
    const href = createPreviewHref(next)
    if (mode === "replace") window.history.replaceState({ paperAgentLab: next }, "", href)
    else window.history.pushState({ paperAgentLab: next }, "", href)
  }, [])

  useEffect(() => {
    const locationState = parsePreviewState(window.location.search)
    previewStateRef.current = locationState
    setPreviewState(locationState)
    const canonicalHref = createPreviewHref(locationState)
    if (`${window.location.pathname}${window.location.search}` !== canonicalHref) {
      window.history.replaceState({ paperAgentLab: locationState }, "", canonicalHref)
    }

    function handlePopState() {
      const next = parsePreviewState(window.location.search)
      previewStateRef.current = next
      setPreviewState(next)
    }

    window.addEventListener("popstate", handlePopState)
    return () => window.removeEventListener("popstate", handlePopState)
  }, [])

  useEffect(() => {
    if (selectedPaper && selectedPaper.id !== previewStateRef.current.paperId) {
      updatePreviewState({ paperId: selectedPaper.id, tab: "overview" }, "replace")
      return
    }
    if (!selectedPaper && (previewState.assistant || previewState.focus)) {
      updatePreviewState({ assistant: false, focus: false }, "replace")
    }
  }, [previewState.assistant, previewState.focus, selectedPaper, updatePreviewState])

  useEffect(() => {
    function handleKeyboard(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === "k" && !isEditableTarget(event.target)) {
        event.preventDefault()
        setSidebarOpen(false)
        setMobileReaderOpen(false)
        updatePreviewState({ assistant: false, focus: false })
        window.requestAnimationFrame(() => {
          window.requestAnimationFrame(() => {
            searchInputRef.current?.focus()
            searchInputRef.current?.select()
          })
        })
        return
      }

      if (event.key !== "Escape") return
      if (previewState.assistant || sidebarOpen) return
      if (previewState.focus) {
        event.preventDefault()
        updatePreviewState({ focus: false })
      }
    }

    window.addEventListener("keydown", handleKeyboard)
    return () => window.removeEventListener("keydown", handleKeyboard)
  }, [previewState.assistant, previewState.focus, sidebarOpen, updatePreviewState])

  useEffect(() => {
    if (!sidebarOpen) return
    const returnTarget = document.activeElement instanceof HTMLElement ? document.activeElement : menuButtonRef.current
    const frame = window.requestAnimationFrame(() => sidebarCloseRef.current?.focus())

    function handleSidebarKeyboard(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault()
        setSidebarOpen(false)
        return
      }
      trapFocus(event, sidebarRef.current)
    }

    window.addEventListener("keydown", handleSidebarKeyboard)
    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener("keydown", handleSidebarKeyboard)
      returnTarget?.focus()
    }
  }, [sidebarOpen])

  useEffect(() => {
    if (!previewState.assistant) return
    const returnTarget = assistantReturnFocusRef.current
    const frame = window.requestAnimationFrame(() => {
      assistantShellRef.current?.querySelector<HTMLElement>('[aria-label="关闭论文助手"]')?.focus()
    })

    function handleAssistantKeyboard(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault()
        updatePreviewState({ assistant: false })
        return
      }
      if (isNarrow) trapFocus(event, assistantShellRef.current)
    }

    window.addEventListener("keydown", handleAssistantKeyboard)
    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener("keydown", handleAssistantKeyboard)
      returnTarget?.focus()
      assistantReturnFocusRef.current = null
    }
  }, [isNarrow, previewState.assistant, updatePreviewState])

  const handleSelectPaper = useCallback((paperId: string) => {
    updatePreviewState({ paperId, tab: "overview" })
    setMobileReaderOpen(true)
  }, [updatePreviewState])

  const handleToggleSave = useCallback((paperId: string) => {
    setSavedIds((current) => {
      const next = new Set(current)
      if (next.has(paperId)) next.delete(paperId)
      else next.add(paperId)
      return next
    })
  }, [])

  const handleToggleRead = useCallback((paperId: string) => {
    setReadIds((current) => {
      const next = new Set(current)
      if (next.has(paperId)) next.delete(paperId)
      else next.add(paperId)
      return next
    })
  }, [])

  const handleViewChange = useCallback((view: LibraryView) => {
    setTopic(null)
    setMobileReaderOpen(false)
    setSidebarOpen(false)
    updatePreviewState({ view })
  }, [updatePreviewState])

  const handleTopicChange = useCallback((nextTopic: TopicId) => {
    setTopic(nextTopic)
    setMobileReaderOpen(false)
    setSidebarOpen(false)
    updatePreviewState({ view: "all" })
  }, [updatePreviewState])

  const handleAsk = useCallback((prompt?: string) => {
    if (!selectedPaper) return
    if (document.activeElement instanceof HTMLElement) assistantReturnFocusRef.current = document.activeElement
    const normalizedPrompt = prompt?.trim()
    setPendingPrompt(normalizedPrompt || undefined)
    updatePreviewState({ assistant: true, focus: false })
  }, [selectedPaper, updatePreviewState])

  const handleSubmitQuestion = useCallback((prompt: string) => {
    const normalizedPrompt = prompt.trim()
    if (!normalizedPrompt || !selectedPaper) return
    questionSequenceRef.current += 1
    setConversation((current) => [
      ...current,
      {
        id: `preview-question-${questionSequenceRef.current}`,
        prompt: normalizedPrompt,
        paperId: selectedPaper.id,
      },
    ])
  }, [selectedPaper])

  const handleFocusToggle = useCallback(() => {
    updatePreviewState({ focus: !previewState.focus, assistant: false })
  }, [previewState.focus, updatePreviewState])

  const handleResetFilters = useCallback(() => {
    setQuery("")
    setTopic(null)
    setMobileReaderOpen(false)
    updatePreviewState({ view: "all", paperId: DEFAULT_PREVIEW_STATE.paperId, tab: "overview" })
  }, [updatePreviewState])

  const assistantIsModal = isNarrow && previewState.assistant
  const navigationIsModal = isNarrow && sidebarOpen
  const backgroundIsInert = assistantIsModal || navigationIsModal

  return (
    <div
      className={styles.root}
      data-assistant={previewState.assistant || undefined}
      data-focus={previewState.focus || undefined}
      data-mobile-reader={mobileReaderOpen || undefined}
      data-testid="paper-agent-lab"
      data-theme={previewState.theme}
    >
      <header className={styles.topbar} inert={backgroundIsInert || undefined}>
        <div className={styles.topbarStart}>
          <button
            aria-expanded={sidebarOpen}
            aria-label="打开导航菜单"
            className={styles.menuButton}
            onClick={() => setSidebarOpen(true)}
            ref={menuButtonRef}
            type="button"
          >
            <Menu aria-hidden="true" size={16} />
          </button>
          <span className={styles.topbarBrand}>Agora <small>Papers</small></span>
          <span className={styles.divider} aria-hidden="true" />
          <span className={styles.currentPaper}>{selectedPaper?.titleZh ?? "当前筛选暂无论文"}</span>
        </div>
        <div className={styles.topbarEnd}>
          <span className={styles.memoryNotice}>界面初版 · 设计预览</span>
          {previewState.focus ? (
            <button aria-label="退出专注阅读（顶栏）" className={styles.textButton} onClick={handleFocusToggle} type="button">
              <PanelLeftClose aria-hidden="true" size={14} />
              退出专注阅读
            </button>
          ) : null}
        </div>
      </header>

      <div className={styles.workspace}>
        <button
          aria-label="关闭导航菜单背景"
          className={styles.sidebarBackdrop}
          onClick={() => setSidebarOpen(false)}
          type="button"
        />
        <div
          aria-label="论文工作台导航"
          aria-modal={sidebarOpen || undefined}
          className={styles.sidebarShell}
          data-open={sidebarOpen || undefined}
          inert={assistantIsModal || undefined}
          role={sidebarOpen ? "dialog" : undefined}
        >
          <WorkspaceSidebar
            allCount={PREVIEW_PAPERS.length}
            closeButtonRef={sidebarCloseRef}
            onClose={() => setSidebarOpen(false)}
            onThemeChange={(theme) => updatePreviewState({ theme })}
            onTopicChange={handleTopicChange}
            onViewChange={handleViewChange}
            ref={sidebarRef}
            savedCount={savedIds.size}
            theme={previewState.theme}
            topic={topic}
            unreadCount={PREVIEW_PAPERS.length - readIds.size}
            view={previewState.view}
          />
        </div>

        <div className={styles.libraryShell} inert={backgroundIsInert || undefined}>
          <LibraryPanel
            heading={listHeading}
            onQueryChange={setQuery}
            onSelectPaper={handleSelectPaper}
            onSortChange={setSort}
            onToggleSave={handleToggleSave}
            papers={filteredPapers}
            query={query}
            readIds={readIds}
            savedIds={savedIds}
            searchInputRef={searchInputRef}
            selectedPaperId={selectedPaper?.id ?? ""}
            sort={sort}
          />
        </div>

        <main className={styles.readerShell} inert={backgroundIsInert || undefined}>
          <button className={styles.mobileBack} onClick={() => setMobileReaderOpen(false)} type="button">
            <ArrowLeft aria-hidden="true" size={15} />
            返回论文列表
          </button>
          {selectedPaper ? (
            <ReaderPanel
              focused={previewState.focus}
              note={notes[selectedPaper.id] ?? ""}
              onAsk={handleAsk}
              onFocusToggle={handleFocusToggle}
              onNoteChange={(value) => setNotes((current) => ({ ...current, [selectedPaper.id]: value }))}
              onTabChange={(tab: ReaderTab) => updatePreviewState({ tab })}
              onToggleRead={() => handleToggleRead(selectedPaper.id)}
              onToggleSave={() => handleToggleSave(selectedPaper.id)}
              paper={selectedPaper}
              read={readIds.has(selectedPaper.id)}
              saved={savedIds.has(selectedPaper.id)}
              tab={previewState.tab}
            />
          ) : (
            <section aria-label="空论文阅读区" className={styles.emptyReader}>
              <span>当前视图</span>
              <h2>没有可显示的论文</h2>
              <p>试试切换视图，或收藏感兴趣的论文。</p>
              <button onClick={handleResetFilters} type="button">返回全部论文</button>
            </section>
          )}
        </main>

        <button
          aria-label="关闭论文助手背景"
          className={styles.assistantBackdrop}
          onClick={() => updatePreviewState({ assistant: false })}
          type="button"
        />
        {previewState.assistant && selectedPaper ? (
          <div
            aria-label="论文助手"
            aria-modal={assistantIsModal || undefined}
            className={styles.assistantShell}
            ref={assistantShellRef}
            role={assistantIsModal ? "dialog" : "complementary"}
          >
            <AssistantPanel
              conversation={conversation}
              initialPrompt={pendingPrompt}
              onClose={() => updatePreviewState({ assistant: false })}
              onPromptConsumed={() => setPendingPrompt(undefined)}
              onSubmit={handleSubmitQuestion}
              paper={selectedPaper}
            />
          </div>
        ) : null}
      </div>

      <footer className={styles.statusbar} inert={backgroundIsInert || undefined}>
        <span>设计预览 · 示例文献 · 未连接研究服务</span>
        <span className={styles.statusDetail}>收藏、已读、笔记与对话仅保留在本次页面内存</span>
      </footer>
    </div>
  )
}
