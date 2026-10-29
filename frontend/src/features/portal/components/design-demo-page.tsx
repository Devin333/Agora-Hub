"use client"

import { useEffect, useRef, useState, useTransition, type FormEvent } from "react"
import { useRouter } from "next/navigation"
import { ArrowUp, BookOpen, Check, ChevronDown, ClipboardCheck, Github, Link2, LoaderCircle, Quote, Sparkles, X, type LucideIcon } from "lucide-react"
import { AppearanceMenu } from "@/components/theme/appearance-menu"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { autoExamples, moduleInfo, researchModules, resolveResearchIntent, type ResearchMode, type ResearchModule } from "@/lib/research/entry"
import { historyState, prepareResearchResume, researchResumeHref, saveResearchMaterial, type ResearchVisit } from "@/lib/research/history"
import { useResearchDraft } from "@/lib/research/use-research-draft"
import { inferResearchConstraints, researchEntryHref } from "@/lib/research/constraints"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import type { ResearchConstraints } from "@/lib/research/workspace-items"
import { useGuidedResearch } from "@/lib/research/use-guided-research"
import { researchPapersHref } from "@/lib/research/routes"
import { cn } from "@/lib/utils"
import { ResearchPromptMenu } from "./research-prompt-menu"
import { ResearchSidebar } from "./research-sidebar"
import { ResearchComposerContext, ResearchContextSummary } from "./research-composer-context"
import { ResearchSourceEntry } from "./research-source-entry"
import { GuidedResearchView } from "./guided-research-view"
import { ResearchCapabilityNotice } from "./research-capability-notice"

const icons: Record<ResearchMode, LucideIcon> = { auto: Sparkles, plan: Sparkles, papers: BookOpen, projects: Github, community: Quote, reports: ClipboardCheck }
const focusRing = "outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
const sidebarPreferenceKey = "agora-research-sidebar-collapsed"

function modeLabel(mode: ResearchMode) { return mode === "auto" ? "自动" : mode === "plan" ? "计划" : moduleInfo[mode].command }
function modeDescription(mode: ResearchMode) { return mode === "auto" ? "根据问题查找相关内容" : mode === "plan" ? "先聊清楚，再帮你查找" : moduleInfo[mode].description }

export function DesignDemoPage() {
  const router = useRouter()
  const { query, setQuery, mode, setMode, constraints, setConstraints, groupId, setGroupId, materialIds, setMaterialIds, ready, restored, clear, markSubmitted, continueEditing } = useResearchDraft()
  const workspace = useOwnedResearchWorkspace()
  const guided = useGuidedResearch()
  const inputRef = useRef<HTMLInputElement>(null)
  const composing = useRef(false)
  const [collapsed, setCollapsed] = useState(false)
  const [destination, setDestination] = useState<ResearchModule | null>(null)
  const [error, setError] = useState("")
  const [pending, startTransition] = useTransition()
  const ModeIcon = icons[mode]
  const examples = mode === "auto" || mode === "plan" ? autoExamples : moduleInfo[mode].examples

  useEffect(() => {
    try { setCollapsed(localStorage.getItem(sidebarPreferenceKey) === "true") } catch { /* Default expanded. */ }
  }, [])
  useEffect(() => {
    if (!destination) return
    const timeout = setTimeout(() => { setError("页面打开较慢，你的问题已保留，请重试。"); setDestination(null) }, 15000)
    return () => clearTimeout(timeout)
  }, [destination])

  function focusInput() {
    inputRef.current?.scrollIntoView({ block: "center", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" })
    inputRef.current?.focus({ preventScroll: true })
  }
  function navigate(module: ResearchModule, href?: string, restoring = false) {
    if (!href) {
      const sessionId = crypto.randomUUID()
      if (!markSubmitted(sessionId)) { setError(historyState().message || "暂时无法保存问题，请重试。"); return }
      href = researchEntryHref(module, query, sessionId, { constraints: Object.keys(constraints).length ? constraints : inferResearchConstraints(query, module), groupId, materialIds })
    }
    const target = href
    setError(""); setDestination(module)
    startTransition(() => {
      try { if (restoring) router.push(target, { scroll: false }); else router.push(target) }
      catch { setDestination(null); setError("暂时无法打开页面，请重试。你的问题已保留。") }
    })
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!query.trim() || composing.current || destination || pending) return
    const candidates = resolveResearchIntent(query, mode)
    if (mode === "community" || mode === "reports" || (mode === "auto" && candidates.length === 1 && (candidates[0] === "community" || candidates[0] === "reports"))) { navigate(candidates[0]); return }
    guided.start(query, mode === "plan" ? "plan" : "auto", groupId, materialIds, constraints, mode === "papers" || mode === "projects" ? [mode] : undefined)
  }
  function resume(visit: ResearchVisit) { if (guided.resume(visit)) return; guided.reset(); prepareResearchResume(visit); navigate(visit.module, researchResumeHref(visit), true) }
  function newResearch() { guided.reset(); clear(); setError(""); setDestination(null); requestAnimationFrame(focusInput) }
  function toggleSidebar() { setCollapsed(value => { const next = !value; try { localStorage.setItem(sidebarPreferenceKey, String(next)) } catch { /* Layout remains usable. */ } return next }) }
  function selectMode(item: ResearchMode) { setMode(item); setConstraints(item === "auto" || item === "plan" ? {} : inferResearchConstraints(query, item)); setError("") }
  const busy = Boolean(!ready || destination || pending)
  const activeTitle = guided.active?.conversation.turns[0]?.question

  return <div className="flex h-svh overflow-hidden bg-background text-foreground">
    <ResearchSidebar busy={busy} collapsed={collapsed} onToggle={toggleSidebar} onNew={newResearch} onResume={resume} onSelectGroup={setGroupId} activeVisitId={guided.active?.id ?? null} />
    <div className="relative isolate flex min-w-0 flex-1 flex-col" aria-label="研究工作区">
      {/* Floating pills over the workspace: the content scrolls underneath, only the pills take input. */}
      <div className="pointer-events-none absolute inset-x-0 top-0 z-10 grid grid-cols-[1fr_minmax(0,auto)_1fr] items-center gap-2 px-4 pt-4 [&_button]:pointer-events-auto">
        <span />
        {activeTitle ? <span className="pointer-events-auto inline-flex h-9 min-w-0 max-w-[min(32rem,60vw)] items-center rounded-full border border-border bg-muted/90 px-3.5 text-[15px] font-medium text-foreground backdrop-blur" title={activeTitle}><span className="truncate">{activeTitle}</span></span> : <span />}
        <div className="flex items-center justify-self-end gap-2"><AppearanceMenu variant="floating" size="icon-lg" /></div>
      </div>
      {guided.active ? <GuidedResearchView research={guided} /> : <div className="rk-scroll min-h-0 flex-1 overflow-y-auto">
        {/* The composer is centered in the workspace; the modules sit below it. */}
        <main id="workspace" className="grid h-full min-h-[560px] grid-rows-[minmax(0,1fr)_auto_minmax(0,1fr)] px-4 md:px-10">
          <h1 className="mx-auto mb-8 w-full max-w-[60rem] self-end pt-[84px] text-center text-4xl font-medium tracking-tight md:text-5xl">Ask. <span className="text-muted-foreground">Discover.</span></h1>
          <section aria-label="研究提问框" className="mx-auto w-full max-w-[60rem]">
            <ResearchCapabilityNotice compact />
            {restored && <div role="status" className="mb-3 flex items-center justify-between gap-3 rounded-[14px] border border-border bg-card px-4 py-2.5 text-body2 dark:bg-input">
              <span>已恢复一条尚未发送的研究问题</span>
              <span className="flex items-center gap-1"><Button variant="ghost" size="sm" onClick={continueEditing}>继续编辑</Button><Button variant="ghost" size="icon-sm" onClick={clear} aria-label="清除未发送的问题"><X /></Button></span>
            </div>}
            <form onSubmit={submit} aria-busy={busy} className="flex min-h-[52px] items-center gap-2 rounded-[26px] border border-border bg-background py-[9px] pe-2.5 ps-3 transition-colors has-[input:focus-visible]:border-ring has-[input:focus-visible]:ring-3 has-[input:focus-visible]:ring-ring/50">
              <ResearchComposerContext key={workspace.owner ?? "guest"} groupId={groupId} setGroupId={setGroupId} materialIds={materialIds} setMaterialIds={setMaterialIds} disabled={busy} />
              <DropdownMenu modal={false}>
                <DropdownMenuTrigger disabled={busy} aria-label="选择研究模式" className={cn("inline-flex h-8 shrink-0 items-center gap-1.5 rounded-full px-2.5 text-body2 font-medium text-foreground/75 transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50 data-[state=open]:bg-accent", focusRing)}>
                  <ModeIcon size={15} strokeWidth={1.8} aria-hidden="true" />{modeLabel(mode)}<ChevronDown size={14} strokeWidth={1.8} aria-hidden="true" className="text-muted-foreground" />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" sideOffset={8} className="w-80 p-1.5">
                  {(["auto", "plan", ...researchModules] as ResearchMode[]).map(item => {
                    const Icon = icons[item]
                    return <DropdownMenuItem key={item} onSelect={() => selectMode(item)} className="items-start gap-3 rounded-lg px-2.5 py-2">
                      <Icon size={16} strokeWidth={1.8} className="mt-0.5 text-muted-foreground" />
                      <span className="min-w-0 flex-1"><span className="block text-body2 font-medium">{modeLabel(item)}</span><span className="mt-0.5 block text-label text-muted-foreground">{modeDescription(item)}</span></span>
                      {mode === item && <Check size={15} strokeWidth={1.8} className="mt-0.5" />}
                    </DropdownMenuItem>
                  })}
                </DropdownMenuContent>
              </DropdownMenu>
              <input ref={inputRef} value={query} maxLength={2000} onChange={event => { setQuery(event.target.value); setError("") }} onCompositionStart={() => { composing.current = true }} onCompositionEnd={() => { composing.current = false }} onKeyDown={event => { if (event.key === "Enter" && (event.nativeEvent.isComposing || event.keyCode === 229 || composing.current)) event.preventDefault() }} aria-label="向 Agora AI 提问" placeholder="输入你想研究的问题…" className="min-w-0 flex-1 bg-transparent px-1 text-body1 text-foreground outline-none placeholder:text-muted-foreground" />
              <ResearchPromptMenu question={query} mode={mode} constraints={constraints} onSelect={prompt => { setQuery(prompt.question); setMode(prompt.mode); setConstraints(prompt.constraints); inputRef.current?.focus() }} />
              <Button type="submit" size="icon" disabled={!query.trim() || busy} aria-label="发送问题" className="size-8 shrink-0 rounded-full transition-transform active:scale-95 disabled:bg-primary-disabled disabled:text-foreground/30 disabled:opacity-100">
                {busy ? <LoaderCircle size={16} strokeWidth={2} className="animate-spin motion-reduce:animate-none" /> : <ArrowUp size={16} strokeWidth={2} />}
              </Button>
            </form>
            <ResearchContextSummary groupId={groupId} setGroupId={setGroupId} materialIds={materialIds} setMaterialIds={setMaterialIds} disabled={busy} />
            {mode !== "auto" && mode !== "plan" && <ConstraintControls module={mode} constraints={constraints} setConstraints={setConstraints} />}
            <div className="mt-4 flex flex-wrap items-center gap-2 px-1">
              <span className="me-1 text-body2 text-muted-foreground">试试：</span>
              {examples.map(prompt => <button key={prompt} disabled={busy} type="button" onClick={() => { setQuery(prompt); setError(""); inputRef.current?.focus() }} className={cn("rounded-full border border-border px-3 py-1.5 text-start text-body2 text-foreground/75 transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50", focusRing)}>{prompt}</button>)}
            </div>
            <SourceHint query={query} />
            <ResearchSourceEntry onAttach={id => setMaterialIds([...new Set([...materialIds, id])])} />
            <div aria-live="polite" role="status" className="text-body2 text-muted-foreground">{destination && <p className="mt-4">正在打开{moduleInfo[destination].name}…</p>}</div>
            {error && <p role="alert" className="mt-4 text-body2 text-destructive">{error}</p>}
          </section>
          <div className="mt-10 min-h-0 self-start">
            <div id="modules" className="mx-auto grid w-full max-w-[60rem] scroll-mt-8 grid-cols-2 gap-3 pb-16">
              {researchModules.map(module => {
                const Icon = icons[module], info = moduleInfo[module]
                return <a key={module} href={info.path} className={cn("flex min-h-[132px] flex-col gap-3 rounded-2xl border border-border bg-card p-4 transition-colors hover:bg-accent/40", focusRing)}>
                  <span className="grid size-9 place-items-center rounded-full bg-muted text-muted-foreground ring-1 ring-foreground/10"><Icon size={16} strokeWidth={1.8} aria-hidden="true" /></span>
                  <span><h2 className="text-body1 font-medium">{info.name}</h2><p className="mt-1 text-body2 text-muted-foreground">{info.description}</p></span>
                </a>
              })}
            </div>
            <footer className="mx-auto flex max-w-[60rem] items-center justify-between border-t border-border py-6 text-label text-faint-foreground"><span>Agora Hub Research</span><span>用 AI 开始你的下一次研究</span></footer>
          </div>
        </main>
      </div>}
    </div>
  </div>
}

function ConstraintControls({ module, constraints, setConstraints }: { module: ResearchModule; constraints: ResearchConstraints; setConstraints: (value: ResearchConstraints) => void }) {
  if (module !== "papers" && module !== "projects") return null
  const options: Array<{ key: keyof ResearchConstraints; label: string; value?: string | boolean }> = module === "papers"
    ? [{ key: "recentYear", label: "近一年", value: true }, { key: "hasCode", label: "有代码", value: true }, { key: "paperType", label: "综述", value: "survey" }]
    : [{ key: "recentlyActive", label: "近期活跃", value: true }, { key: "language", label: "Python 项目", value: "python" }, { key: "license", label: "MIT 许可", value: "MIT" }]
  return <div className="mt-3 flex flex-wrap items-center gap-2 px-1 text-body2">
    <span className="text-muted-foreground">筛选：</span>
    {options.map(item => {
      const active = constraints[item.key] === item.value
      return <button key={item.key} type="button" aria-pressed={active} onClick={() => setConstraints({ ...constraints, [item.key]: active ? undefined : item.value })} className={cn("inline-flex items-center gap-1 rounded-full border px-3 py-1 transition-colors", focusRing, active ? "border-transparent bg-primary text-primary-foreground hover:bg-primary-hover" : "border-border text-foreground/75 hover:bg-accent hover:text-foreground")}>{item.label}{active && <X size={12} strokeWidth={2} aria-hidden="true" />}</button>
    })}
  </div>
}

function SourceHint({ query }: { query: string }) {
  const workspace = useOwnedResearchWorkspace()
  const [saved, setSaved] = useState(false)
  const [groupId, setGroupId] = useState("")
  const arxiv = query.match(/https?:\/\/(?:www\.)?arxiv\.org\/(?:abs|pdf)\/[^\s]+/i)?.[0]
  const github = query.match(/https?:\/\/github\.com\/[\w.-]+\/[\w.-]+/i)?.[0]
  const doi = query.match(/https?:\/\/doi\.org\/[^\s]+/i)?.[0]
  const source = arxiv || github || doi
  if (!source) return null
  const resolvedSource = source
  const related = `${researchPapersHref()}?q=${encodeURIComponent(query.replace(resolvedSource, "").trim())}&entry=source`
  function save() {
    if (!workspace.ready || !workspace.workspace.groups.some(group => group.id === groupId)) return
    const kind = resolvedSource.includes("github.com") ? "project" : "paper"
    setSaved(saveResearchMaterial({ id: crypto.randomUUID(), groupId, kind, title: resolvedSource.split("/").filter(Boolean).pop() || resolvedSource, url: resolvedSource, notes: "从首页来源识别保存", createdAt: Date.now(), updatedAt: Date.now() }, workspace.owner))
  }
  return <div className="mt-4 flex flex-wrap items-center gap-2 rounded-[14px] border border-border bg-card px-3 py-2.5 text-body2 text-muted-foreground dark:bg-input" role="status">
    <Link2 size={15} strokeWidth={1.8} aria-hidden="true" />
    <span>识别到来源：</span>
    <a href={resolvedSource} target="_blank" rel="noreferrer" className={cn("max-w-[330px] truncate rounded-sm text-link underline underline-offset-2", focusRing)}>{resolvedSource}</a>
    <Button asChild variant="outline" size="sm"><a href={related}>查找相关研究</a></Button>
    {workspace.workspace.groups.length > 0 && <>
      <select aria-label="来源保存分组" className={cn("h-7 rounded-md border border-input bg-muted px-2 text-body2 text-foreground", focusRing)} value={groupId} onChange={event => setGroupId(event.target.value)}><option value="">选择分组</option>{workspace.workspace.groups.map(group => <option key={group.id} value={group.id}>{group.name}</option>)}</select>
      <Button type="button" variant="outline" size="sm" disabled={!groupId || saved} onClick={save}>{saved ? "已保存" : "保存来源"}</Button>
    </>}
  </div>
}
