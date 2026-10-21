"use client"

import { useEffect, useId, useRef, type KeyboardEvent, type ReactNode } from "react"
import {
  BookOpenCheck,
  Bookmark,
  Bot,
  Check,
  ExternalLink,
  Focus,
  MessageCircle,
} from "lucide-react"
import type { PreviewPaper, ReaderTab, TopicId } from "./types"
import styles from "./reader-panel.module.css"

const READER_TABS: readonly { id: ReaderTab; label: string }[] = [
  { id: "overview", label: "概览" },
  { id: "notes", label: "笔记" },
  { id: "details", label: "详情" },
]

const TOPIC_LABELS: Record<TopicId, string> = {
  memory: "记忆系统",
  retrieval: "检索增强",
  foundation: "基础模型",
}

export interface ReaderPanelProps {
  paper: PreviewPaper
  tab: ReaderTab
  onTabChange: (tab: ReaderTab) => void
  saved: boolean
  onToggleSave: () => void
  note: string
  onNoteChange: (value: string) => void
  read: boolean
  onToggleRead: () => void
  onAsk: (prompt?: string) => void
  onFocusToggle: () => void
  focused: boolean
}

export function ReaderPanel({
  paper,
  tab,
  onTabChange,
  saved,
  onToggleSave,
  note,
  onNoteChange,
  read,
  onToggleRead,
  onAsk,
  onFocusToggle,
  focused,
}: ReaderPanelProps) {
  const tabListId = useId()
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])
  const scrollerRef = useRef<HTMLDivElement>(null)
  const noteLength = Array.from(note).length

  useEffect(() => {
    if (scrollerRef.current) scrollerRef.current.scrollTop = 0
  }, [paper.id, tab])

  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>, currentIndex: number) {
    let nextIndex: number | undefined

    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % READER_TABS.length
    if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + READER_TABS.length) % READER_TABS.length
    if (event.key === "Home") nextIndex = 0
    if (event.key === "End") nextIndex = READER_TABS.length - 1
    if (nextIndex === undefined) return

    event.preventDefault()
    const nextTab = READER_TABS[nextIndex]
    onTabChange(nextTab.id)
    tabRefs.current[nextIndex]?.focus()
  }

  return (
    <section className={styles.reader} aria-label={`${paper.title} 阅读区`}>
      <header className={styles.toolbar}>
        <div className={styles.tabs} id={tabListId} role="tablist" aria-label="论文阅读视图">
          {READER_TABS.map((item, index) => {
            const selected = tab === item.id
            return (
              <button
                key={item.id}
                ref={(node) => { tabRefs.current[index] = node }}
                aria-controls={`${tabListId}-${item.id}-panel`}
                aria-selected={selected}
                className={styles.tab}
                id={`${tabListId}-${item.id}-tab`}
                onClick={() => onTabChange(item.id)}
                onKeyDown={(event) => handleTabKeyDown(event, index)}
                role="tab"
                tabIndex={selected ? 0 : -1}
                type="button"
              >
                {item.label}
              </button>
            )
          })}
        </div>

        <div className={styles.actions} aria-label="论文操作">
          <ToolbarAction
            active={read}
            label={read ? "标为未读" : "标为已读"}
            onClick={onToggleRead}
          >
            {read ? <Check aria-hidden="true" /> : <BookOpenCheck aria-hidden="true" />}
          </ToolbarAction>
          <ToolbarAction
            active={saved}
            label={saved ? `取消收藏：${paper.title}` : `收藏：${paper.title}`}
            onClick={onToggleSave}
          >
            <Bookmark aria-hidden="true" fill={saved ? "currentColor" : "none"} />
          </ToolbarAction>
          <ToolbarAction label="打开论文助手" onClick={() => onAsk()}>
            <Bot aria-hidden="true" />
          </ToolbarAction>
          <ToolbarAction
            active={focused}
            label={focused ? "退出专注阅读" : "专注阅读"}
            onClick={onFocusToggle}
          >
            <Focus aria-hidden="true" />
          </ToolbarAction>
        </div>
      </header>

      <div className={styles.scroller} ref={scrollerRef}>
        {tab === "overview" ? (
          <OverviewPanel paper={paper} panelId={`${tabListId}-overview-panel`} tabId={`${tabListId}-overview-tab`} onAsk={onAsk} />
        ) : null}
        {tab === "notes" ? (
          <NotesPanel
            note={note}
            noteLength={noteLength}
            onNoteChange={onNoteChange}
            panelId={`${tabListId}-notes-panel`}
            paper={paper}
            tabId={`${tabListId}-notes-tab`}
          />
        ) : null}
        {tab === "details" ? (
          <DetailsPanel paper={paper} panelId={`${tabListId}-details-panel`} tabId={`${tabListId}-details-tab`} />
        ) : null}
      </div>
    </section>
  )
}

interface ToolbarActionProps {
  active?: boolean
  children: ReactNode
  label: string
  onClick: () => void
}

function ToolbarAction({ active, children, label, onClick }: ToolbarActionProps) {
  return (
    <button
      aria-label={label}
      aria-pressed={active}
      className={styles.actionButton}
      data-active={active || undefined}
      onClick={onClick}
      title={label}
      type="button"
    >
      {children}
    </button>
  )
}

interface OverviewPanelProps {
  paper: PreviewPaper
  panelId: string
  tabId: string
  onAsk: (prompt?: string) => void
}

function OverviewPanel({ paper, panelId, tabId, onAsk }: OverviewPanelProps) {
  const question = `请解释《${paper.titleZh}》的核心机制，以及它对后续研究的启发。`

  return (
    <article
      aria-labelledby={tabId}
      className={styles.document}
      id={panelId}
      role="tabpanel"
      tabIndex={0}
    >
      <div className={styles.guideLine}>
        <span>文献导读 / 预置示例</span>
        <a href={paper.sourceUrl} target="_blank" rel="noopener noreferrer">
          arXiv:{paper.arxivId}
          <ExternalLink aria-hidden="true" />
        </a>
      </div>

      <header className={styles.paperHeader}>
        <h1>{paper.title}</h1>
        <p className={styles.chineseTitle}>{paper.titleZh}</p>
        <p className={styles.byline}>{paper.authors}</p>
        <p className={styles.publication}>{paper.venue} · {paper.year}</p>
      </header>

      <section className={styles.ledeSection} aria-labelledby={`${panelId}-summary`}>
        <h2 id={`${panelId}-summary`}>导读</h2>
        <p className={styles.lede}>{paper.summary}</p>
        <p className={styles.boundaryNote}>本页使用公开文献信息展示阅读排版，不是论文全文，也未连接研究服务。</p>
      </section>

      <section className={styles.keyPoints} aria-labelledby={`${panelId}-points`}>
        <div className={styles.sectionHeadingRow}>
          <h2 id={`${panelId}-points`}>三个重点</h2>
          <span>{String(paper.keyPoints.length).padStart(2, "0")}</span>
        </div>
        <ol>
          {paper.keyPoints.map((point, index) => (
            <li key={`${paper.id}-point-${index}`}>
              <span aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
              <p>{point}</p>
            </li>
          ))}
        </ol>
      </section>

      {paper.sections.map((section, sectionIndex) => (
        <section className={styles.proseSection} key={`${paper.id}-${section.heading}`}>
          <h2>{section.heading}</h2>
          {section.paragraphs.map((paragraph, paragraphIndex) => (
            <p key={`${paper.id}-${sectionIndex}-${paragraphIndex}`}>{paragraph}</p>
          ))}
          {sectionIndex === 0 && paper.keyPoints[0] ? (
            <blockquote>
              <p>{paper.keyPoints[0]}</p>
              <cite>导读摘记（非原文引文）</cite>
            </blockquote>
          ) : null}
        </section>
      ))}

      <footer className={styles.documentFooter}>
        <div>
          <span>下一步</span>
          <p>把阅读中的疑问带到右侧论文助手。</p>
        </div>
        <button type="button" onClick={() => onAsk(question)}>
          <MessageCircle aria-hidden="true" />
          继续追问
        </button>
      </footer>
    </article>
  )
}

interface NotesPanelProps {
  paper: PreviewPaper
  note: string
  noteLength: number
  onNoteChange: (value: string) => void
  panelId: string
  tabId: string
}

function NotesPanel({ paper, note, noteLength, onNoteChange, panelId, tabId }: NotesPanelProps) {
  const noteId = `${panelId}-note`
  const helpId = `${panelId}-help`

  return (
    <div aria-labelledby={tabId} className={styles.utilityPanel} id={panelId} role="tabpanel" tabIndex={0}>
      <header className={styles.utilityHeader}>
        <span>阅读笔记</span>
        <h1>{paper.titleZh}</h1>
        <p>记录论点、疑问和准备回看的线索。</p>
      </header>
      <div className={styles.noteField}>
        <div className={styles.noteLabelRow}>
          <label htmlFor={noteId}>本篇笔记</label>
          <span aria-live="polite">输入字数：{noteLength}</span>
        </div>
        <textarea
          aria-describedby={helpId}
          id={noteId}
          onChange={(event) => onNoteChange(event.target.value)}
          placeholder="例如：作者把长期上下文拆分成哪些可管理的记忆层？"
          rows={14}
          value={note}
        />
        <p id={helpId}>仅本次预览保留，不写入长期记忆。</p>
      </div>
    </div>
  )
}

interface DetailsPanelProps {
  paper: PreviewPaper
  panelId: string
  tabId: string
}

function DetailsPanel({ paper, panelId, tabId }: DetailsPanelProps) {
  return (
    <div aria-labelledby={tabId} className={styles.utilityPanel} id={panelId} role="tabpanel" tabIndex={0}>
      <header className={styles.utilityHeader}>
        <span>文献详情</span>
        <h1>{paper.title}</h1>
        <p>以下字段来自当前固定公开论文样本。</p>
      </header>

      <dl className={styles.detailsList}>
        <DetailRow label="中文题名" value={paper.titleZh} />
        <DetailRow label="作者" value={paper.authors} />
        <DetailRow label="出版信息" value={`${paper.venue} · ${paper.year}`} />
        <DetailRow label="arXiv" value={paper.arxivId} mono />
        <DetailRow label="主题" value={TOPIC_LABELS[paper.topic]} />
        <div className={styles.detailRow}>
          <dt>标签</dt>
          <dd className={styles.tagList}>
            {paper.tags.map((tag) => <span key={tag}>{tag}</span>)}
          </dd>
        </div>
        <div className={styles.detailRow}>
          <dt>来源</dt>
          <dd>
            <a className={styles.sourceLink} href={paper.sourceUrl} target="_blank" rel="noopener noreferrer">
              <span>{paper.sourceUrl}</span>
              <ExternalLink aria-hidden="true" />
            </a>
          </dd>
        </div>
      </dl>

      <a className={styles.openPaper} href={paper.sourceUrl} target="_blank" rel="noopener noreferrer">
        打开原论文
        <ExternalLink aria-hidden="true" />
      </a>
    </div>
  )
}

function DetailRow({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className={styles.detailRow}>
      <dt>{label}</dt>
      <dd className={mono ? styles.mono : undefined}>{value}</dd>
    </div>
  )
}


