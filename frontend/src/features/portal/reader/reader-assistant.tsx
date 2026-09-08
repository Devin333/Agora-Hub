"use client"

import { useEffect, useRef, useState } from "react"
import { ArrowUp, BookOpen, Download, FilePenLine, Sparkles, Square, Quote } from "lucide-react"
import ReactMarkdown from "react-markdown"
import { askPaper } from "@/lib/papers/api"
import type { Paper, PaperReaderAnswer } from "@/lib/papers/types"
import type { useReaderNotebook } from "./use-reader-notebook"
import styles from "./reader-workspace.module.css"

export function safeReaderSourceUrl(value?: string) {
  try {
    const url = new URL(value || "")
    return ["http:", "https:"].includes(url.protocol) ? url.href : undefined
  } catch { return undefined }
}

type Notebook = ReturnType<typeof useReaderNotebook>
const prompts = ["这篇论文解决了什么问题？", "核心方法与已有工作有什么不同？", "实验支持了哪些结论，有什么局限？"]

export function ReaderAssistant({ paper, notebook, sectionIds, onNavigate, researchQuestion, unavailableMessage }: {
  paper: Paper; notebook: Notebook; sectionIds: Set<string>; onNavigate: (id: string) => void; researchQuestion: string; unavailableMessage?: string
}) {
  const [tab, setTab] = useState<"ask" | "notes">("ask")
  const [answer, setAnswer] = useState<PaperReaderAnswer | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const request = useRef<AbortController | null>(null)
  const questionRef = useRef<HTMLTextAreaElement>(null)
  const replyRef = useRef<HTMLDivElement>(null)
  useEffect(() => () => { request.current?.abort() }, [])

  async function submit() {
    const question = notebook.question.trim()
    if (!question || pending || unavailableMessage) return
    const controller = new AbortController()
    request.current = controller
    let timedOut = false
    const timeout = window.setTimeout(() => { timedOut = true; controller.abort() }, 60000)
    setPending(true)
    setError("")
    try {
      const result = await askPaper(paper.id, question, "zh", { signal: controller.signal })
      if (!controller.signal.aborted) {
        setAnswer(result)
        window.requestAnimationFrame(() => replyRef.current?.focus())
      }
    } catch {
      if (request.current === controller) setError(timedOut ? "回答等待超时，问题已保留，请稍后重试。" : controller.signal.aborted ? "已停止回答，问题已保留。" : "暂时无法回答，问题已保留。可以重试，或先查看原文。")
    } finally {
      window.clearTimeout(timeout)
      if (request.current === controller) {
        setPending(false)
        request.current = null
      }
    }
  }

  function exportNote() {
    const content = `# ${paper.title}\n\n${safeReaderSourceUrl(paper.pdfUrl) || ""}\n\n## 阅读笔记\n\n${notebook.note}\n`
    const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }))
    const link = document.createElement("a")
    link.href = url
    link.download = `${paper.id.replace(/[^a-zA-Z0-9_-]/g, "-")}-notes.md`
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  return <aside className={styles.assistant} aria-label="阅读助手">
    <div className={styles.assistantHeading}><span className={styles.spark}><Sparkles size={19} /></span><div><strong>阅读助手</strong><p>理解、追问，留下你的思考</p></div></div>
    <div className={styles.assistantTabs} role="group" aria-label="助手工具">
      <button type="button" aria-pressed={tab === "ask"} onClick={() => setTab("ask")}><Sparkles size={15} />问论文</button>
      <button type="button" aria-pressed={tab === "notes"} onClick={() => setTab("notes")}><FilePenLine size={15} />阅读笔记{notebook.note && <span className={styles.noteDot} aria-label="有笔记" />}</button>
    </div>
    <div hidden={tab !== "ask"} className={styles.askPanel}>
      <div className={styles.assistantContent}>
        {answer ? <div className={styles.answer} ref={replyRef} tabIndex={-1}>
          <p className={styles.askedQuestion}><Quote size={15} />{answer.question}</p>
          <ReactMarkdown>{answer.answer}</ReactMarkdown>
          <div className={styles.evidence}><strong>回答依据 · {answer.citations.length} 条</strong>
            {answer.citations.length ? answer.citations.map((citation) => <details key={citation.id}>
              <summary>{citation.label}</summary>
              {citation.textExcerpt && <blockquote>{citation.textExcerpt}</blockquote>}
              {citation.sectionId && sectionIds.has(citation.sectionId) ? <button type="button" onClick={() => onNavigate(citation.sectionId!)}>定位章节 <BookOpen size={14} /></button> : safeReaderSourceUrl(citation.url) ? <a href={safeReaderSourceUrl(citation.url)} target="_blank" rel="noreferrer">查看来源 ↗</a> : <small>请结合原文核对这条依据。</small>}
            </details>) : <p>本次回答未附来源，请结合原文核对。</p>}
          </div>
        </div> : <>
          <div className={styles.assistantIntro}><h2>带着问题，读得更深。</h2><p>从一个具体问题开始，回答后可以展开依据，回到原文核对。</p></div>
          <div className={styles.suggestions}>
            <span>你可以这样问</span>
            {prompts.map((prompt, index) => <button type="button" key={prompt} disabled={!notebook.ready || pending} onClick={() => { notebook.update({ question: prompt }); questionRef.current?.focus() }}><span>0{index + 1}</span>{prompt}</button>)}
          </div>
          {researchQuestion && <button type="button" className={styles.researchQuestion} disabled={!notebook.ready || pending} onClick={() => { notebook.update({ question: researchQuestion }); questionRef.current?.focus() }}><small>继续你的研究问题</small>{researchQuestion}</button>}
        </>}
      </div>
      <form className={styles.askForm} onSubmit={(event) => { event.preventDefault(); void submit() }}>
        <label htmlFor="paper-reader-question">向这篇论文提问</label>
        {unavailableMessage && <p className={styles.unavailableAsk} role="status">{unavailableMessage} 问题可以先保留。</p>}
        <div className={styles.questionInput}>
          <textarea id="paper-reader-question" ref={questionRef} value={notebook.question} disabled={!notebook.ready || pending} maxLength={2000} placeholder="例如：这个方法适合什么场景？" onChange={(event) => notebook.update({ question: event.target.value })} />
          <div><span>{pending ? "正在查阅论文…" : "仅围绕当前论文"}</span>{pending ? <button type="button" aria-label="停止回答" onClick={() => request.current?.abort()}><Square size={15} /></button> : <button type="submit" aria-label="发送问题" disabled={!notebook.ready || !notebook.question.trim() || Boolean(unavailableMessage)}><ArrowUp size={19} /></button>}</div>
        </div>
        {error && <p role="alert" className={styles.error}>{error}</p>}
        {notebook.storageError && <p role="status" className={styles.error}>问题草稿未能保存到浏览器，请复制后再离开。</p>}
        <p className={styles.footnote}>AI 回答仅供辅助阅读，请核对引用原文。</p>
      </form>
    </div>
    <div hidden={tab !== "notes"} className={styles.notesPanel}>
      <label htmlFor="paper-reader-note">我的阅读笔记</label><p>记录理解、疑问和下一步想验证的事。</p>
      <textarea id="paper-reader-note" value={notebook.note} disabled={!notebook.ready} maxLength={20000} placeholder="这篇论文对我的研究有什么启发？" onChange={(event) => notebook.update({ note: event.target.value })} />
      <p role="status" className={notebook.storageError ? styles.error : styles.footnote}>{!notebook.ready ? "正在读取笔记…" : notebook.storageError ? "浏览器未能保存，请导出笔记后再离开。" : "自动保存在当前浏览器 · 不跨设备同步"}</p>
      <button type="button" className={styles.exportButton} disabled={!notebook.note.trim()} onClick={exportNote}><Download size={16} />导出 Markdown</button>
      <p className={styles.footnote}>正文划词批注可直接点击原文中的高亮查看。</p>
    </div>
  </aside>
}
