"use client"

import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from "react"
import { ArrowUp, Bot, FileText, Sparkles, X } from "lucide-react"
import type { PreviewPaper, PreviewQuestion } from "./types"
import styles from "./assistant-panel.module.css"

const SUGGESTIONS = [
  "这篇论文试图解决什么问题？",
  "请拆解它的核心机制与信息流。",
  "阅读这篇论文时需要留意哪些限制？",
] as const

export interface AssistantPanelProps {
  paper: PreviewPaper
  conversation: readonly PreviewQuestion[]
  onSubmit: (prompt: string) => void
  onClose: () => void
  initialPrompt?: string
  onPromptConsumed?: () => void
}

export function AssistantPanel({
  paper,
  conversation,
  onSubmit,
  onClose,
  initialPrompt,
  onPromptConsumed,
}: AssistantPanelProps) {
  const [draft, setDraft] = useState("")
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const consumedPromptRef = useRef<string | null>(null)
  const composerId = useId()
  const visibleConversation = conversation.filter((question) => question.paperId === paper.id)

  useEffect(() => {
    setDraft("")
    consumedPromptRef.current = null
  }, [paper.id])

  useEffect(() => {
    const prompt = initialPrompt?.trim() ?? ""
    if (!prompt) {
      consumedPromptRef.current = null
      return
    }

    const promptKey = `${paper.id}\u0000${prompt}`
    if (consumedPromptRef.current === promptKey) return

    consumedPromptRef.current = promptKey
    setDraft(prompt)
    onPromptConsumed?.()
    window.requestAnimationFrame(() => inputRef.current?.focus())
  }, [initialPrompt, onPromptConsumed, paper.id])

  function focusComposer() {
    window.requestAnimationFrame(() => inputRef.current?.focus())
  }

  function fillSuggestion(prompt: string) {
    setDraft(prompt)
    focusComposer()
  }

  function submitDraft() {
    const prompt = draft.trim()
    if (!prompt) return
    onSubmit(prompt)
    setDraft("")
    focusComposer()
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    submitDraft()
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) return
    if (event.nativeEvent.isComposing || event.keyCode === 229) return
    event.preventDefault()
    submitDraft()
  }

  return (
    <aside className={styles.panel} aria-label="论文助手">
      <header className={styles.header}>
        <div className={styles.heading}>
          <Bot aria-hidden="true" />
          <div>
            <span>论文助手</span>
            <strong>阅读预览</strong>
          </div>
        </div>
        <button aria-label="关闭论文助手" className={styles.closeButton} onClick={onClose} type="button">
          <X aria-hidden="true" />
        </button>
      </header>

      <div className={styles.currentPaper}>
        <FileText aria-hidden="true" />
        <div>
          <span>当前论文</span>
          <p title={paper.title}>{paper.titleZh}</p>
        </div>
      </div>

      <div className={styles.conversation} aria-label="预览对话">
        {visibleConversation.length === 0 ? (
          <div className={styles.emptyState}>
            <span className={styles.emptyIcon} aria-hidden="true"><Sparkles /></span>
            <h2>一起读懂这篇论文</h2>
            <p>选择一个阅读方向，或写下你真正想追问的问题。这里只演示交互，不会调用模型。</p>
            <div className={styles.suggestions} aria-label="问题建议">
              {SUGGESTIONS.map((suggestion) => (
                <button key={suggestion} onClick={() => fillSuggestion(suggestion)} type="button">
                  <span>{suggestion}</span>
                  <ArrowUp aria-hidden="true" />
                </button>
              ))}
            </div>
          </div>
        ) : (
          <ol className={styles.thread} aria-live="polite" aria-relevant="additions" role="log">
            {visibleConversation.map((question) => (
              <li key={question.id}>
                <div className={styles.questionMeta}>
                  <span>你的问题</span>
                </div>
                <p className={styles.question}>{question.prompt}</p>
                <div className={styles.previewNotice}>
                  <Bot aria-hidden="true" />
                  <p>预览模式：问题未发送，尚未连接研究服务。</p>
                </div>
              </li>
            ))}
          </ol>
        )}
      </div>

      <form className={styles.composer} onSubmit={handleSubmit}>
        <label htmlFor={composerId}>向论文助手提问</label>
        <div className={styles.inputShell}>
          <textarea
            ref={inputRef}
            id={composerId}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={handleComposerKeyDown}
            placeholder="输入问题，Enter 提交，Shift + Enter 换行"
            rows={3}
            value={draft}
          />
          <button aria-label="提交预览问题" disabled={!draft.trim()} type="submit">
            <ArrowUp aria-hidden="true" />
          </button>
        </div>
        <p>仅预览交互，不调用模型。</p>
      </form>
    </aside>
  )
}




