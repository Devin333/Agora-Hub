"use client"

import { ArrowUp, BookOpen, Check, ChevronDown, ExternalLink, Github, LoaderCircle, RotateCcw, Square } from "lucide-react"
import { useLayoutEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react"
import type { useGuidedResearch } from "@/lib/research/use-guided-research"
import { conversationHref, type ResearchIntent, type ResearchResult, type ResearchSource } from "@/lib/research/conversation"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
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
const eventLabels = { understanding: "开始理解问题", clarifying: "等待补充", confirming: "等待确认", searching: "开始查找", results: "收到查找结果", stopped: "查找暂停", error: "本轮未完成" } as const

/**
 * The transcript and the composer share one column: 60rem of content inside
 * padding wide enough to keep the 32px conversation index clear of it.
 */
const column = "mx-auto w-full max-w-[calc(60rem+5rem)] px-9 md:px-10"
const focusRing = "outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
const userBubble = "ms-auto w-fit max-w-[80%] whitespace-pre-wrap break-words rounded-[20px] bg-chat-user px-[18px] py-3 text-body1 text-chat-user-foreground"
const botBubble = "w-full max-w-[44rem] rounded-[20px] bg-muted px-[18px] py-3.5 text-body1 text-foreground"
const chipClass = cn("inline-flex min-h-8 items-center gap-1.5 rounded-full border border-border bg-background px-3 py-1 text-body2 text-foreground/75 transition-colors hover:bg-accent hover:text-foreground aria-pressed:border-transparent aria-pressed:bg-primary aria-pressed:text-primary-foreground", focusRing)
const textAction = cn("ms-2 rounded-sm text-body2 text-link underline-offset-2 hover:underline", focusRing)

function ProgressCard({ phase, sources, onStop }: { phase: keyof typeof phaseLabels; sources: ResearchSource[]; onStop: () => void }) {
  const searching = phase === "searching"
  const currentIndex = searching ? 2 : 1
  const steps = ["已收到问题", "正在整理需求", `查找${sources.map(source => labels[source]).join("和")}`, "整理结果"]
  return <div className={cn(botBubble, "mt-6")} role="status" aria-live="polite">
    <div className="flex items-center justify-between gap-3">
      <span className="inline-flex items-center gap-2 text-body2 font-medium"><LoaderCircle size={16} strokeWidth={2} className="animate-spin text-status-working motion-reduce:animate-none" aria-hidden="true" /><span className="text-shimmer">{phaseLabels[phase]}</span></span>
      <Button type="button" variant="outline" size="sm" className="rounded-full bg-background" onClick={onStop}><Square size={11} strokeWidth={2} fill="currentColor" aria-hidden="true" />停止</Button>
    </div>
    <ol className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-label md:grid-cols-4">
      {steps.map((step, index) => <li key={step} className={cn("flex min-w-0 items-center gap-1.5 whitespace-nowrap", index < currentIndex ? "text-foreground/75" : index === currentIndex ? "font-medium text-foreground" : "text-faint-foreground")}>
        <span aria-hidden="true" className={cn("grid size-4 shrink-0 place-items-center rounded-full text-[10px]", index < currentIndex ? "bg-foreground/10" : index === currentIndex ? "bg-status-working text-background" : "border border-border")}>{index < currentIndex ? <Check size={10} strokeWidth={2.5} /> : null}</span>
        <span className="truncate">{step}</span>
      </li>)}
    </ol>
  </div>
}

/** Bordered notice used for empty, stopped and failed rounds. */
function TurnNotice({ tone = "muted", children, role }: { tone?: "muted" | "error"; children: ReactNode; role?: "alert" }) {
  return <div role={role} className={cn("mt-5 rounded-[14px] border px-4 py-3.5 text-body2", tone === "error" ? "border-destructive/30 bg-destructive/5 text-destructive-text" : "border-dashed border-border text-muted-foreground")}>{children}</div>
}

export function GuidedResearchView({ research }: { research: Research }) {
  const input = useRef<HTMLTextAreaElement>(null)
  const transcript = useRef<HTMLDivElement>(null)
  const active = research.active
  const conversationId = active?.id, turns = active?.conversation.turns
  const draft = active?.conversation.draft
  useLayoutEffect(() => {
    const field = input.current
    if (!field) return
    const fitContent = () => { field.style.height = "auto"; field.style.height = `${field.scrollHeight}px` }
    fitContent()
    let width = field.clientWidth
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => {
      if (field.clientWidth !== width) { width = field.clientWidth; fitContent() }
    })
    observer?.observe(field)
    return () => observer?.disconnect()
  }, [conversationId, draft])
  const entries = useMemo(() => turns?.flatMap(turn => {
    const id = `research-message-${conversationId}-${turn.id}`
    return [{ id, label: turn.question }, ...turn.answers.map((answer, index) => ({ id: `${id}-answer-${index}`, label: answer }))]
  }) ?? [], [conversationId, turns])
  if (!active) return null
  const last = active.conversation.turns.at(-1)!
  const canSend = !research.busy && Boolean(active.conversation.draft.trim())
  function submit(event: FormEvent) {
    event.preventDefault()
    if (canSend && active) research.followUp(active.conversation.draft)
  }
  const firstQuestion = active.conversation.turns[0].question
  return <main className="flex min-h-0 w-full min-w-0 flex-1 flex-col overflow-hidden" aria-label="当前研究">
    <div className="relative flex min-h-0 w-full min-w-0 flex-1">
      <ResearchConversationIndex key={active.id} entries={entries} transcriptRef={transcript} />
      <div ref={transcript} className={cn("rk-scroll min-h-0 w-full min-w-0 flex-1 overflow-y-auto scroll-py-6 [scrollbar-gutter:stable_both-edges]", focusRing)} role="region" aria-label="研究对话内容" tabIndex={0}>
        <div className={cn(column, "pb-8 pt-20 md:pt-[84px]")}>
          <header id={entries[0].id} className="flex scroll-mt-20 justify-end outline-none" tabIndex={-1} data-testid="research-transcript-surface">
            <h1 className={cn(userBubble, "font-normal")}>{firstQuestion}</h1>
          </header>
          <div className="grid gap-10 pb-4 pt-6">
            {active.conversation.turns.map((turn, index) => {
              const current = index === active.conversation.turns.length - 1
              const messageId = `research-message-${active.id}-${turn.id}`
              return <section key={turn.id} className="flex min-w-0 animate-fade-in flex-col" aria-label={`第 ${index + 1} 轮研究`}>
                {index > 0 && <div id={messageId} className={cn(userBubble, "scroll-mt-20 outline-none")} tabIndex={-1}>{turn.question}</div>}
                {turn.answers.map((answer, i) => <p id={`${messageId}-answer-${i}`} key={i} className={cn(userBubble, "mt-3 scroll-mt-20 outline-none")} tabIndex={-1}>补充：{answer}</p>)}
                {turn.intent?.clarification && turn.phase === "clarifying" && current && <div className={cn(botBubble, "mt-6")}>
                  <p>{turn.intent.clarification.question}</p>
                  <div className="mt-3 flex flex-wrap gap-2">{turn.intent.clarification.options.map(option => <button type="button" key={option} className={chipClass} onClick={() => research.followUp(option)}>{option}</button>)}</div>
                  <div className="mt-4 flex flex-wrap justify-end gap-2"><Button type="button" variant="outline" className="rounded-full bg-background" onClick={() => input.current?.focus()}>我再补充</Button><Button type="button" className="rounded-full" onClick={research.skip}>先帮我找找</Button></div>
                </div>}
                {turn.intent && turn.phase === "confirming" && current && <IntentConfirmation key={turn.id} intent={turn.intent} onChange={research.editIntent} onRevise={research.followUp} onConfirm={research.confirm} />}
                {current && research.busy && <ProgressCard phase={turn.phase as "understanding" | "searching"} sources={turn.intent?.sources ?? ["papers", "projects"]} onStop={research.stop} />}
                {turn.searches.map(search => <section key={search.source} className="mt-8" aria-label={`${labels[search.source]}查找结果`}>
                  <div className="mb-3 flex items-baseline gap-3"><h2 className="text-heading3 font-semibold">{labels[search.source]}</h2><span className="text-body2 text-muted-foreground">{search.total} 条相关结果</span></div>
                  {!search.results.length
                    ? <TurnNotice><p>暂时没找到符合这些条件的{labels[search.source]}。</p>{current && <div className="mt-3 flex flex-wrap gap-2"><button type="button" className={chipClass} onClick={() => { research.setDraft("扩大时间范围，时间不限"); input.current?.focus() }}>扩大时间范围</button><button type="button" className={chipClass} onClick={() => input.current?.focus()}>补充或修改问题</button></div>}</TurnNotice>
                    : <ol className="grid gap-3">{search.results.map((result, position) => <li key={result.id}><ResultCard result={result} position={position + 1} returnTo={conversationHref(active.id)} /></li>)}</ol>}
                  {search.moreHref && search.total > search.results.length && <a href={search.moreHref} target="_blank" rel="noreferrer" className={cn("mt-3 inline-flex items-center gap-1.5 rounded-sm text-body2 text-link underline-offset-2 hover:underline", focusRing)}>查看更多{labels[search.source]}<ExternalLink size={14} strokeWidth={1.8} aria-hidden="true" /></a>}
                </section>)}
                {turn.failures.map(failure => <TurnNotice key={failure.source} tone="error" role="alert"><p>{labels[failure.source]}：{failure.message}</p>{current && !research.busy && <Button type="button" variant="outline" size="sm" className="mt-3 rounded-full bg-background text-foreground" onClick={() => research.retry(failure.source)}><RotateCcw aria-hidden="true" />重试{labels[failure.source]}</Button>}</TurnNotice>)}
                {turn.phase === "error" && <TurnNotice tone="error" role="alert"><p>{turn.error || "这次查找没有完成，问题已保留。"}</p>{current && <Button type="button" variant="outline" size="sm" className="mt-3 rounded-full bg-background text-foreground" onClick={() => research.retry()}><RotateCcw aria-hidden="true" />重试</Button>}</TurnNotice>}
                {turn.phase === "stopped" && current && <TurnNotice><p>查找已暂停，已有内容已保留。</p><Button type="button" variant="outline" size="sm" className="mt-3 rounded-full bg-background text-foreground" onClick={() => research.retry()}>继续查找</Button></TurnNotice>}
                {turn.phase === "results" && !turn.failures.length && <p className="mt-5 flex items-center gap-1.5 text-body2 text-muted-foreground"><Check size={15} strokeWidth={2} className="text-success-text" aria-hidden="true" />本轮查找完成，可以继续补充要求。</p>}
                <details className="group/events mt-4 text-label text-muted-foreground">
                  <summary className={cn("inline-flex w-fit cursor-pointer list-none items-center gap-1 rounded-sm hover:text-foreground [&::-webkit-details-marker]:hidden", focusRing)}>查看查找进展<ChevronDown size={13} strokeWidth={1.8} aria-hidden="true" className="transition-transform group-open/events:rotate-180" /></summary>
                  <ol className="mt-2 grid gap-1 border-s border-border ps-3">{turn.events.map((event, i) => <li key={i} className="flex gap-3"><span>{eventLabels[event.phase]}</span><time className="tabular-nums text-faint-foreground">{new Date(event.at).toLocaleTimeString("zh-CN")}</time></li>)}</ol>
                </details>
              </section>
            })}
          </div>
        </div>
      </div>
    </div>
    <form className="rk-scroll max-h-[50%] min-h-0 w-full min-w-0 shrink-0 overflow-y-auto bg-background [scrollbar-gutter:stable_both-edges]" onSubmit={submit} aria-label="继续研究输入区">
      <div className={cn(column, "pb-5 pt-2")}>
        <div data-testid="research-composer-surface" className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-end gap-x-3.5 rounded-[26px] border border-border bg-background py-[9px] pe-2.5 ps-3 transition-colors has-[textarea:focus-visible]:border-ring has-[textarea:focus-visible]:ring-3 has-[textarea:focus-visible]:ring-ring/50">
          <ResearchComposerContext groupId={active.groupId} setGroupId={research.setGroupId} materialIds={active.conversation.materialIds} setMaterialIds={research.setMaterialIds} disabled={research.busy} />
          <textarea id="research-follow-up" ref={input} aria-label={last.phase === "clarifying" ? "补充你的想法" : "继续这次研究"} rows={1} maxLength={2000} value={active.conversation.draft} onChange={e => research.setDraft(e.target.value)} placeholder={last.phase === "clarifying" ? "也可以直接告诉我你的想法…" : "补充要求，或针对某一条结果继续提问…"} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); if (canSend) research.followUp(active.conversation.draft) } }} className="rk-scroll max-h-[180px] min-h-8 w-full resize-none self-center overflow-y-auto bg-transparent py-1 text-body1 text-foreground shadow-none [outline:none] placeholder:text-muted-foreground" />
          <Button type="submit" size="icon" disabled={!canSend} aria-label="发送补充" className="size-8 shrink-0 rounded-full transition-transform active:scale-95 disabled:bg-primary-disabled disabled:text-foreground/30 disabled:opacity-100">
            {research.busy ? <LoaderCircle size={16} strokeWidth={2} className="animate-spin motion-reduce:animate-none" /> : <ArrowUp size={16} strokeWidth={2} />}
          </Button>
        </div>
        <ResearchContextSummary groupId={active.groupId} setGroupId={research.setGroupId} materialIds={active.conversation.materialIds} setMaterialIds={research.setMaterialIds} disabled={research.busy} />
        <ResearchSourceEntry onAttach={id => research.setMaterialIds([...new Set([...active.conversation.materialIds, id])])} />
        {last.phase === "results" && !research.busy && <div className="mt-3 flex flex-wrap items-center gap-2 px-1 text-body2 text-muted-foreground" aria-label="推荐继续提问">
          <span>推荐继续</span>
          {["比较前两条结果", "只看带代码的项目", "换一个方向继续找"].map(suggestion => <button type="button" key={suggestion} className={chipClass} onClick={() => research.setDraft(suggestion)}>{suggestion}</button>)}
        </div>}
        {research.saveError && <p role="alert" className="mt-2 px-1 text-body2 text-destructive-text">{research.saveError}</p>}
      </div>
    </form>
  </main>
}

function IntentConfirmation({ intent, onChange, onRevise, onConfirm }: { intent: ResearchIntent; onChange: (value: Partial<ResearchIntent>) => void; onRevise: (question: string) => void; onConfirm: () => void }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(intent.summary)
  const [adjustTypes, setAdjustTypes] = useState(intent.sources.length > 1)
  const conditions = [intent.constraints.recentYear && "最近一年", intent.constraints.hasCode && "附带代码", intent.constraints.localRunnable && "可以本地运行", intent.constraints.recentlyActive && "近期活跃", intent.constraints.language, intent.constraints.license, intent.constraints.paperType === "survey" && "综述论文"].filter(Boolean).join(" · ")
  return <div className={cn(botBubble, "mt-6")} role="region" aria-label="确认研究计划">
    {intent.changeNotice && <p className="mb-2 text-body2 text-muted-foreground">{intent.changeNotice}</p>}
    {editing
      ? <label className="grid gap-2 text-body2 font-medium">我想找<textarea value={text} maxLength={500} onChange={e => setText(e.target.value)} className="rk-scroll min-h-[90px] w-full resize-y rounded-xl border border-input bg-background px-3 py-2.5 text-body1 font-normal text-foreground outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50" /></label>
      : <p>我会帮你找：<strong className="font-medium">{intent.summary}</strong></p>}
    {(intent.sources.length > 1 || adjustTypes)
      ? <div className="mt-3 flex flex-wrap items-center gap-2" role="group" aria-label="想看哪些"><span className="me-1 text-body2 text-muted-foreground">想看哪些？</span>{(["papers", "projects", "both"] as const).map(source => {
        const selected = source === "both" ? intent.sources.length === 2 : intent.sources.length === 1 && intent.sources[0] === source
        return <button type="button" key={source} aria-pressed={selected} className={chipClass} onClick={() => onChange({ sources: source === "both" ? ["papers", "projects"] : [source] })}>{source === "both" ? "都看看" : labels[source]}</button>
      })}</div>
      : <p className="mt-3 text-body2 text-muted-foreground">查找{labels[intent.sources[0]]}<button type="button" className={textAction} onClick={() => setAdjustTypes(true)}>调整</button></p>}
    {conditions && <p className="mt-3 text-body2 text-muted-foreground">{conditions}<button type="button" className={textAction} onClick={() => onChange({ constraints: {} })}>清除条件</button></p>}
    <div className="mt-4 flex flex-wrap justify-end gap-2">
      <Button type="button" variant="outline" className="rounded-full bg-background" onClick={() => { if (editing) { if (!text.trim()) return; if (text.trim() !== intent.summary) onRevise(text.trim()) }; setEditing(!editing) }}>{editing ? "保存修改" : "改一下"}</Button>
      <Button type="button" className="rounded-full px-4" disabled={editing} onClick={onConfirm}>开始查找</Button>
    </div>
  </div>
}

function ResultCard({ result, position, returnTo }: { result: ResearchResult; position: number; returnTo: string }) {
  const [expanded, setExpanded] = useState(false)
  const Icon = result.kind === "papers" ? BookOpen : Github
  const sourceLabel = result.kind === "papers" ? "打开原文" : new URL(result.url).hostname === "github.com" ? "打开 GitHub" : "打开来源"
  const href = result.href ? new URL(result.href, "https://agora.invalid") : null
  if (href) href.searchParams.set("returnTo", returnTo)
  const metadata = [result.source, result.authors, result.publishedAt?.slice(0, 10), result.language, result.stars !== undefined ? `${result.stars} Stars` : ""].filter(Boolean).join(" · ")
  return <article className="rounded-2xl border border-border bg-card p-4 transition-colors hover:bg-accent/30 md:px-5">
    <div className="flex items-start gap-3">
      <span className="grid size-8 shrink-0 place-items-center rounded-full bg-muted text-muted-foreground ring-1 ring-foreground/10"><Icon size={15} strokeWidth={1.8} aria-hidden="true" /></span>
      <div className="min-w-0 flex-1">
        <h3 className="text-body1 font-medium leading-snug"><span className="me-1.5 tabular-nums text-faint-foreground">{position}.</span><a href={href ? href.pathname + href.search : result.url} target="_blank" rel="noreferrer" className={cn("rounded-sm underline-offset-2 hover:underline", focusRing)}>{result.title}</a></h3>
        {metadata && <p className="mt-1 text-label text-muted-foreground">{metadata}</p>}
        <p className={cn("mt-2 break-words text-body2 text-foreground/75", !expanded && "line-clamp-3")}>{result.description || "打开来源查看完整内容。"}</p>
        {result.description.length > 240 && <button type="button" className={cn("mt-1 rounded-sm text-label text-link underline-offset-2 hover:underline", focusRing)} aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? "收起简介" : "展开简介"}</button>}
        <div className="mt-3 flex flex-wrap gap-2">
          {href && <Button asChild variant="outline" size="sm" className="rounded-full"><a href={href.pathname + href.search} target="_blank" rel="noreferrer">{result.kind === "papers" ? href.pathname.endsWith("/read") ? "阅读全文" : "查看论文" : "查看项目"}</a></Button>}
          <Button asChild variant="outline" size="sm" className="rounded-full"><a href={result.url} target="_blank" rel="noreferrer">{sourceLabel}<ExternalLink aria-hidden="true" /></a></Button>
        </div>
      </div>
    </div>
  </article>
}
