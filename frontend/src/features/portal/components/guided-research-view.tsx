"use client"

import { ArrowUp, BookOpen, Check, ExternalLink, Github, LoaderCircle, RotateCcw, Square } from "lucide-react"
import { useMemo, useRef, useState, type FormEvent } from "react"
import type { useGuidedResearch } from "@/lib/research/use-guided-research"
import { conversationHref, type ResearchIntent, type ResearchResult, type ResearchSource } from "@/lib/research/conversation"
import styles from "./guided-research.module.css"
import { ResearchComposerContext, ResearchContextSummary } from "./research-composer-context"
import { ResearchSourceEntry } from "./research-source-entry"
import { ResearchConversationIndex } from "./research-conversation-index"

type Research = ReturnType<typeof useGuidedResearch>
const labels: Record<ResearchSource, string> = { papers: "论文", projects: "开源项目" }
const phaseLabels = {
  understanding: "正在整理需求",
  clarifying: "等待补充信息",
  confirming: "等待确认范围",
  searching: "正在查找资料",
  results: "本轮查找完成",
  stopped: "查找已暂停",
  error: "本轮未完成",
} as const

function ProgressCard({ phase, sources, onStop }: { phase: keyof typeof phaseLabels; sources: ResearchSource[]; onStop: () => void }) {
  const searching = phase === "searching"
  const currentIndex = searching ? 2 : 1
  const steps = ["已收到问题", "正在整理需求", `查找${sources.map(source => labels[source]).join("和")}`, "整理结果"]
  return <div className={styles.progressCard} role="status" aria-live="polite">
    <div className={styles.progressHeader}>
      <span className={styles.progressTitle}><LoaderCircle size={18} className={styles.spinner} />{phaseLabels[phase]}</span>
      <button type="button" onClick={onStop}><Square size={13} />停止</button>
    </div>
    <ol className={styles.progressSteps}>
      {steps.map((step, index) => <li key={step} className={index < currentIndex ? styles.stepDone : index === currentIndex ? styles.stepCurrent : styles.stepPending}>
        <span aria-hidden="true">{index < currentIndex ? "✓" : index === currentIndex ? "●" : "○"}</span>{step}
      </li>)}
    </ol>
  </div>
}

export function GuidedResearchView({ research }: { research: Research }) {
  const input = useRef<HTMLTextAreaElement>(null)
  const transcript = useRef<HTMLDivElement>(null)
  const active = research.active
  const conversationId = active?.id, turns = active?.conversation.turns
  const entries = useMemo(() => turns?.flatMap(turn => {
    const id = `research-message-${conversationId}-${turn.id}`
    return [{ id, label: turn.question }, ...turn.answers.map((answer, index) => ({ id: `${id}-answer-${index}`, label: answer }))]
  }) ?? [], [conversationId, turns])
  if (!active) return null
  const last = active.conversation.turns.at(-1)!
  function submit(event: FormEvent) {
    event.preventDefault()
    if (!research.busy && active?.conversation.draft.trim()) research.followUp(active.conversation.draft)
  }
  const firstQuestion = active.conversation.turns[0].question
  return <main className={styles.workspace} aria-label="当前研究">
    <div className={styles.readingPane}>
    <ResearchConversationIndex key={active.id} entries={entries} transcriptRef={transcript} />
    <div ref={transcript} className={styles.transcript} role="region" aria-label="研究对话内容" tabIndex={0}>
    <header id={entries[0].id} className={styles.intro} tabIndex={-1}>
      <div className={styles.introEyebrow}><span className={styles.phaseBadge}>{phaseLabels[last.phase]}</span></div>
      <h1>{firstQuestion}</h1>
    </header>
    <div className={styles.turns}>
      {active.conversation.turns.map((turn, index) => {
        const current = index === active.conversation.turns.length - 1
        const messageId = `research-message-${active.id}-${turn.id}`
        return <section key={turn.id} className={styles.turn} aria-label={`第 ${index + 1} 轮研究`}>
          {index > 0 && <div id={messageId} className={styles.question} tabIndex={-1}>{turn.question}</div>}
          {turn.answers.map((answer, i) => <p id={`${messageId}-answer-${i}`} key={i} className={styles.answer} tabIndex={-1}>补充：{answer}</p>)}
          {turn.intent?.clarification && turn.phase === "clarifying" && current && <div className={styles.clarification}>
            <p>{turn.intent.clarification.question}</p>
            <div className={styles.choices}>{turn.intent.clarification.options.map(option => <button type="button" key={option} onClick={() => research.followUp(option)}>{option}</button>)}</div>
            <div className={styles.actions}><button type="button" onClick={() => input.current?.focus()}>我再补充</button><button type="button" onClick={research.skip}>先帮我找找</button></div>
          </div>}
          {turn.intent && turn.phase === "confirming" && current && <IntentConfirmation key={turn.id} intent={turn.intent} onChange={research.editIntent} onRevise={research.followUp} onConfirm={research.confirm} />}
          {current && research.busy && <ProgressCard phase={turn.phase as "understanding" | "searching"} sources={turn.intent?.sources ?? ["papers", "projects"]} onStop={research.stop} />}
          {turn.searches.map(search => <section key={search.source} className={styles.resultSection} aria-label={`${labels[search.source]}查找结果`}>
            <div className={styles.sectionHeading}><h2>{labels[search.source]}</h2><span>{search.total} 条相关结果</span></div>
            {!search.results.length ? <div className={styles.empty}><p>暂时没找到符合这些条件的{labels[search.source]}。</p>{current && <div className={styles.actions}><button type="button" onClick={() => { research.setDraft("扩大时间范围，时间不限"); input.current?.focus() }}>扩大时间范围</button><button type="button" onClick={() => input.current?.focus()}>补充或修改问题</button></div>}</div> : <ol className={styles.results}>{search.results.map((result, position) => <li key={result.id}><ResultCard result={result} position={position + 1} returnTo={conversationHref(active.id)} /></li>)}</ol>}
            {search.moreHref && search.total > search.results.length && <a href={search.moreHref} target="_blank" rel="noreferrer" className={styles.more}>查看更多{labels[search.source]}<ExternalLink size={14} /></a>}
          </section>)}
          {turn.failures.map(failure => <div key={failure.source} className={styles.error} role="alert"><p>{labels[failure.source]}：{failure.message}</p>{current && !research.busy && <button type="button" onClick={() => research.retry(failure.source)}><RotateCcw size={15} />重试{labels[failure.source]}</button>}</div>)}
          {turn.phase === "error" && <div className={styles.error} role="alert"><p>{turn.error || "这次查找没有完成，问题已保留。"}</p>{current && <button type="button" onClick={() => research.retry()}><RotateCcw size={15} />重试</button>}</div>}
          {turn.phase === "stopped" && current && <div className={styles.stopped}><p>查找已暂停，已有内容已保留。</p><button type="button" onClick={() => research.retry()}>继续查找</button></div>}
          {turn.phase === "results" && !turn.failures.length && <p className={styles.complete}><Check size={15} />本轮查找完成，可以继续补充要求。</p>}
          <details className={styles.events}><summary>查看查找进展</summary><ol>{turn.events.map((event, i) => <li key={i}>{({ understanding: "开始理解问题", clarifying: "等待补充", confirming: "等待确认", searching: "开始查找", results: "收到查找结果", stopped: "查找暂停", error: "本轮未完成" })[event.phase]}<time>{new Date(event.at).toLocaleTimeString("zh-CN")}</time></li>)}</ol></details>
        </section>
      })}
    </div>
    </div>
    </div>
    <form className={styles.composer} onSubmit={submit} aria-label="继续研究输入区">
      <label htmlFor="research-follow-up">{last.phase === "clarifying" ? "补充你的想法" : "继续这次研究"}</label>
      <div className={styles.inputRow}><ResearchComposerContext groupId={active.groupId} setGroupId={research.setGroupId} materialIds={active.conversation.materialIds} setMaterialIds={research.setMaterialIds} disabled={research.busy} /><textarea id="research-follow-up" ref={input} rows={1} maxLength={2000} value={active.conversation.draft} onChange={e => research.setDraft(e.target.value)} placeholder={last.phase === "clarifying" ? "也可以直接告诉我你的想法…" : "补充要求，或针对某一条结果继续提问…"} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); if (!research.busy && active.conversation.draft.trim()) research.followUp(active.conversation.draft) } }} /><button type="submit" disabled={research.busy || !active.conversation.draft.trim()} aria-label="发送补充"><ArrowUp size={21} /></button></div>
      <ResearchContextSummary groupId={active.groupId} setGroupId={research.setGroupId} materialIds={active.conversation.materialIds} setMaterialIds={research.setMaterialIds} disabled={research.busy} />
      <ResearchSourceEntry onAttach={id => research.setMaterialIds([...new Set([...active.conversation.materialIds, id])])} />
      {last.phase === "results" && !research.busy && <div className={styles.suggestions} aria-label="推荐继续提问">
        <span>推荐继续</span>
        {["比较前两条结果", "只看带代码的项目", "换一个方向继续找"].map(suggestion => <button type="button" key={suggestion} onClick={() => research.setDraft(suggestion)}>{suggestion}</button>)}
      </div>}
      {research.saveError && <p role="alert" className={styles.saveError}>{research.saveError}</p>}
    </form>
  </main>
}

function IntentConfirmation({ intent, onChange, onRevise, onConfirm }: { intent: ResearchIntent; onChange: (value: Partial<ResearchIntent>) => void; onRevise: (question: string) => void; onConfirm: () => void }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(intent.summary)
  const [adjustTypes, setAdjustTypes] = useState(intent.sources.length > 1)
  return <div className={styles.confirmation} role="region" aria-label="确认研究计划">
    {intent.changeNotice && <p className={styles.conditions}>{intent.changeNotice}</p>}
    {editing ? <label className={styles.editLabel}>我想找<textarea value={text} maxLength={500} onChange={e => setText(e.target.value)} /></label> : <p>我会帮你找：<strong>{intent.summary}</strong></p>}
    {(intent.sources.length > 1 || adjustTypes) ? <div className={styles.choices} role="group" aria-label="想看哪些"><span>想看哪些？</span>{(["papers", "projects", "both"] as const).map(source => {
      const selected = source === "both" ? intent.sources.length === 2 : intent.sources.length === 1 && intent.sources[0] === source
      return <button type="button" key={source} aria-pressed={selected} onClick={() => onChange({ sources: source === "both" ? ["papers", "projects"] : [source] })}>{source === "both" ? "都看看" : labels[source]}</button>
    })}</div> : <p className={styles.conditions}>查找{labels[intent.sources[0]]}<button type="button" onClick={() => setAdjustTypes(true)}>调整</button></p>}
    {Object.values(intent.constraints).some(Boolean) && <p className={styles.conditions}>{[intent.constraints.recentYear && "最近一年", intent.constraints.hasCode && "附带代码", intent.constraints.localRunnable && "可以本地运行", intent.constraints.recentlyActive && "近期活跃", intent.constraints.language, intent.constraints.license, intent.constraints.paperType === "survey" && "综述论文"].filter(Boolean).join(" · ")}<button type="button" onClick={() => onChange({ constraints: {} })}>清除条件</button></p>}
    <div className={styles.actions}><button type="button" onClick={() => { if (editing) { if (!text.trim()) return; if (text.trim() !== intent.summary) onRevise(text.trim()) }; setEditing(!editing) }}>{editing ? "保存修改" : "改一下"}</button><button type="button" className={styles.primary} disabled={editing} onClick={onConfirm}>开始查找</button></div>
  </div>
}

function ResultCard({ result, position, returnTo }: { result: ResearchResult; position: number; returnTo: string }) {
  const [expanded, setExpanded] = useState(false)
  const Icon = result.kind === "papers" ? BookOpen : Github
  const sourceLabel = result.kind === "papers" ? "打开原文" : new URL(result.url).hostname === "github.com" ? "打开 GitHub" : "打开来源"
  const href = result.href ? new URL(result.href, "https://agora.invalid") : null
  if (href) href.searchParams.set("returnTo", returnTo)
  return <article className={styles.result}>
    <div className={styles.resultTitle}><span className={styles.resultNumber}>{position}</span><Icon size={19} /><h3><a href={href ? href.pathname + href.search : result.url} target="_blank" rel="noreferrer">{result.title}</a></h3></div>
    <p className={styles.metadata}>{[result.source, result.authors, result.publishedAt?.slice(0, 10), result.language, result.stars !== undefined ? `${result.stars} Stars` : ""].filter(Boolean).join(" · ")}</p>
    <p className={`${styles.description} ${expanded ? "" : styles.collapsedDescription}`}>{result.description || "打开来源查看完整内容。"}</p>
    {result.description.length > 240 && <button type="button" className={styles.expandDescription} aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? "收起简介" : "展开简介"}</button>}
    <div className={styles.links}>{href && <a href={href.pathname + href.search} target="_blank" rel="noreferrer">{result.kind === "papers" ? href.pathname.endsWith("/read") ? "阅读全文" : "查看论文" : "查看项目"}</a>}<a href={result.url} target="_blank" rel="noreferrer">{sourceLabel}<ExternalLink size={14} /></a></div>
  </article>
}
