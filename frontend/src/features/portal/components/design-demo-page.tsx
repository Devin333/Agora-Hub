"use client"

import { useEffect, useRef, useState, useTransition, type FormEvent } from "react"
import { useRouter } from "next/navigation"
import { ArrowRight, BookOpen, Check, ChevronDown, ClipboardCheck, Github, LoaderCircle, Quote, Sparkles, WandSparkles, X, Link2 } from "lucide-react"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { autoExamples, moduleInfo, researchModules, resolveResearchIntent, type ResearchMode, type ResearchModule } from "@/lib/research/entry"
import { historyState, prepareResearchResume, researchResumeHref, saveResearchMaterial, type ResearchVisit } from "@/lib/research/history"
import { useResearchDraft } from "@/lib/research/use-research-draft"
import { inferResearchConstraints, researchEntryHref } from "@/lib/research/constraints"
import { ResearchPromptMenu } from "./research-prompt-menu"
import { ResearchSidebar } from "./research-sidebar"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import type { ResearchConstraints } from "@/lib/research/workspace-items"
import { ResearchComposerContext, ResearchContextSummary } from "./research-composer-context"
import { ResearchSourceEntry } from "./research-source-entry"
import { useGuidedResearch } from "@/lib/research/use-guided-research"
import { GuidedResearchView } from "./guided-research-view"

const icons = { auto: Sparkles, plan: Sparkles, papers: BookOpen, projects: Github, community: Quote, reports: ClipboardCheck }
const focusStyle = "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#8b5cf6] focus-visible:ring-offset-2"

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
    try { setCollapsed(localStorage.getItem("agora-research-sidebar-collapsed") === "true") } catch { /* Default expanded. */ }
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
  function toggleSidebar() { setCollapsed(value => { const next = !value; try { localStorage.setItem("agora-research-sidebar-collapsed", String(next)) } catch { /* Layout remains usable. */ } return next }) }
  const busy = Boolean(!ready || destination || pending)

  return <div className="h-screen overflow-hidden bg-[#fbf8ff] font-papers-research text-[#211a3c] [--header-height:73px]">
    <header className="h-[var(--header-height)] border-b border-[#eee8f5] bg-white">
      <nav className="mx-auto flex h-full max-w-[1280px] items-center justify-between px-10" aria-label="主导航">
        <a href="/design-demo" className={`flex items-center gap-2.5 rounded-lg ${focusStyle}`}><span className="flex size-9 items-center justify-center rounded-[10px] bg-[#7c3aed] text-white shadow-[0_5px_12px_rgba(124,58,237,0.22)]"><WandSparkles className="size-[18px]" /></span><span className="text-[19px] font-bold text-[#2b2148]">Agora<span className="text-[#7c3aed]">AI</span></span></a>
      </nav>
    </header>
    <div className="flex h-[calc(100svh-var(--header-height))]">
    <ResearchSidebar busy={busy} collapsed={collapsed} onToggle={toggleSidebar} onNew={newResearch} onResume={resume} onSelectGroup={setGroupId} />
    <div className={`min-w-0 flex-1 ${guided.active ? "overflow-hidden" : "overflow-y-auto"}`} aria-label="研究工作区">
    {guided.active ? <GuidedResearchView research={guided} /> : <>
    {/* The composer is centered within the right workspace, accounting for the header. */}
    <main id="workspace" className="grid h-[calc(100svh-var(--header-height)-var(--header-height))] min-h-[520px] grid-rows-[minmax(0,1fr)_auto_minmax(0,1fr)]">
      <h1 className="mx-auto w-[calc(100%-80px)] max-w-[900px] self-start pt-[clamp(5rem,12vh,9rem)] text-center font-sans text-[56px] font-bold leading-[1.2]"><span className="text-[#35274f]">Ask.</span>{" "}<span className="text-[#7c3aed]">Discover.</span></h1>
      <section aria-label="研究提问框" className="relative mx-auto w-[calc(100%-80px)] max-w-[960px] rounded-3xl border border-[#e7dff1] bg-white p-8 shadow-[0_18px_45px_rgba(86,58,127,0.13)]">
        {restored && <div role="status" className="mb-4 flex items-center justify-between rounded-xl bg-[#f6f0fd] px-4 py-3 text-sm text-[#684b82]"><span>已恢复一条尚未发送的研究问题</span><span className="flex items-center gap-3"><button type="button" onClick={continueEditing} className={`rounded-md px-2 py-1 text-[#6d28d9] ${focusStyle}`}>继续编辑</button><button type="button" onClick={clear} aria-label="清除未发送的问题" className={`rounded-md ${focusStyle}`}><X size={16} /></button></span></div>}
        <form onSubmit={submit} aria-busy={busy} className="flex min-h-[72px] flex-wrap items-center gap-3 rounded-[24px] border border-[#ded4ec] bg-[#fefeff] p-2.5 pl-3 shadow-[0_3px_12px_rgba(86,58,127,0.06)] focus-within:border-[#a783ec] focus-within:ring-2 focus-within:ring-[#f0e9ff]">
          <ResearchComposerContext key={workspace.owner ?? "guest"} groupId={groupId} setGroupId={setGroupId} materialIds={materialIds} setMaterialIds={setMaterialIds} disabled={busy} />
          <DropdownMenu modal={false}><DropdownMenuTrigger disabled={busy} aria-label="选择研究模式" className={`inline-flex h-11 shrink-0 items-center gap-2 rounded-xl px-3 text-base font-semibold text-[#5b4c76] hover:bg-[#f3edff] ${focusStyle}`}><span className="flex size-8 items-center justify-center rounded-lg bg-[#f0e9ff] text-[#7c3aed]"><ModeIcon size={18} /></span>{mode === "auto" ? "自动" : mode === "plan" ? "计划" : moduleInfo[mode].command}<ChevronDown size={16} /></DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-80 rounded-2xl border-[#e4d9ef] bg-white p-2 text-[#3f3158] shadow-[0_14px_36px_rgba(60,41,93,0.16)]">
              {(["auto", "plan", ...researchModules] as ResearchMode[]).map(item => { const Icon = icons[item]; const label = item === "auto" ? "自动" : item === "plan" ? "计划" : moduleInfo[item].command; const description = item === "auto" ? "根据问题查找相关内容" : item === "plan" ? "先聊清楚，再帮你查找" : moduleInfo[item].description; return <DropdownMenuItem key={item} onSelect={() => { setMode(item); setConstraints(item === "auto" || item === "plan" ? {} : inferResearchConstraints(query, item)); setError("") }} className="flex cursor-pointer items-center gap-3 rounded-xl px-3 py-3 focus:bg-[#f3edff]"><Icon className="size-5 shrink-0 text-[#7c3aed]" /><span className="flex-1"><span className="block text-base font-semibold">{label}</span><span className="mt-1 block text-sm text-[#766885]">{description}</span></span>{mode === item && <Check className="size-4 text-[#7c3aed]" />}</DropdownMenuItem> })}
            </DropdownMenuContent>
          </DropdownMenu>
          <input ref={inputRef} value={query} maxLength={2000} onChange={event => { setQuery(event.target.value); setError("") }} onCompositionStart={() => { composing.current = true }} onCompositionEnd={() => { composing.current = false }} onKeyDown={event => { if (event.key === "Enter" && (event.nativeEvent.isComposing || event.keyCode === 229 || composing.current)) event.preventDefault() }} aria-label="向 Agora AI 提问" placeholder="输入你想研究的问题…" className="min-w-0 flex-1 bg-transparent px-1 text-lg text-[#3a304f] outline-none placeholder:text-[#93879f]" />
          <ResearchPromptMenu question={query} mode={mode} constraints={constraints} onSelect={prompt => { setQuery(prompt.question); setMode(prompt.mode); setConstraints(prompt.constraints); inputRef.current?.focus() }} />
          <button disabled={!query.trim() || busy} type="submit" aria-label="发送问题" className={`flex size-12 shrink-0 items-center justify-center rounded-full bg-[#7c3aed] text-white transition-colors hover:bg-[#6d28d9] disabled:cursor-not-allowed disabled:bg-[#c3abea] ${focusStyle}`}>{busy ? <LoaderCircle className="size-5 animate-spin motion-reduce:animate-none" /> : <ArrowRight size={21} />}</button>
        </form>
        <ResearchContextSummary groupId={groupId} setGroupId={setGroupId} materialIds={materialIds} setMaterialIds={setMaterialIds} disabled={busy} />
        {mode !== "auto" && mode !== "plan" && <ConstraintControls module={mode} constraints={constraints} setConstraints={setConstraints} />}
        <div className="mt-5 flex flex-wrap items-center gap-2.5 px-1"><span className="mr-1 text-[15px] text-[#756982]">试试：</span>{examples.map(prompt => <button key={prompt} disabled={busy} type="button" onClick={() => { setQuery(prompt); setError(""); inputRef.current?.focus() }} className={`rounded-xl border border-[#e7dff1] bg-white px-3.5 py-2.5 text-left text-[15px] leading-5 text-[#6b607e] transition-colors hover:border-[#b99beb] hover:bg-[#faf7ff] disabled:opacity-60 ${focusStyle}`}>{prompt}</button>)}</div>
        <SourceHint query={query} />
        <ResearchSourceEntry onAttach={id => setMaterialIds([...new Set([...materialIds, id])])} />
        <div aria-live="polite" role="status" className="text-[15px] text-[#6d28d9]">{destination && <p className="mt-4">正在打开{moduleInfo[destination].name}…</p>}</div>
        {error && <p role="alert" className="mt-4 text-[15px] text-[#a02b47]">{error}</p>}
      </section>
      <div className="mt-10 min-h-0 self-start bg-[#fbf8ff]">
        <div id="modules" className="mx-auto grid w-[calc(100%-80px)] max-w-[960px] scroll-mt-8 grid-cols-2 gap-6 pb-20">
          {researchModules.map(module => { const Icon = icons[module]; const info = moduleInfo[module]; return <a key={module} href={info.path} className={`group flex min-h-[226px] flex-col items-center justify-center rounded-2xl border border-white/75 bg-white/50 p-7 text-center shadow-[0_18px_45px_rgba(86,58,127,0.08)] backdrop-blur-xl transition-colors hover:border-[#d8c6f0] hover:bg-white/75 ${focusStyle}`}><span className="flex size-[72px] items-center justify-center rounded-2xl bg-[#f0e9ff]/85 text-[#7c3aed]"><Icon className="size-8" /></span><h2 className="mt-5 text-[22px] font-semibold text-[#372b51]">{info.name}</h2><p className="mt-2 text-[17px] text-[#756782]">{info.description}</p></a> })}
        </div>
        <footer className="px-10 py-10"><div className="mx-auto flex max-w-[1280px] items-center justify-between text-base text-[#82758f]"><span>Agora Hub Research</span><span>用 AI 开始你的下一次研究</span></div></footer>
      </div>
    </main>
    </>}
    </div>
    </div>
  </div>
}

function ConstraintControls({ module, constraints, setConstraints }: { module: ResearchModule; constraints: ResearchConstraints; setConstraints: (value: ResearchConstraints) => void }) {
  if (module !== "papers" && module !== "projects") return null
  const options: Array<{ key: keyof ResearchConstraints; label: string; value?: string | boolean }> = module === "papers"
    ? [{ key: "recentYear", label: "近一年", value: true }, { key: "hasCode", label: "有代码", value: true }, { key: "paperType", label: "综述", value: "survey" }]
    : [{ key: "recentlyActive", label: "近期活跃", value: true }, { key: "language", label: "Python 项目", value: "python" }, { key: "license", label: "MIT 许可", value: "MIT" }]
  return <div className="mt-4 flex flex-wrap items-center gap-2 text-sm"><span className="text-[#827190]">筛选：</span>{options.map(item => { const active = constraints[item.key] === item.value; return <button key={item.key} type="button" aria-pressed={active} onClick={() => setConstraints({ ...constraints, [item.key]: active ? undefined : item.value })} className={`rounded-full border px-3 py-1.5 ${active ? "border-[#9a6ee0] bg-[#f0e9ff] text-[#6d28d9]" : "border-[#e7dff1] bg-white text-[#756782]"}`}>{item.label} {active ? "×" : ""}</button> })}</div>
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
  const related = `/design-demo/papers?q=${encodeURIComponent(query.replace(resolvedSource, "").trim())}&entry=source`
  function save() {
    if (!workspace.ready || !workspace.workspace.groups.some(group => group.id === groupId)) return
    const kind = resolvedSource.includes("github.com") ? "project" : "paper"
    setSaved(saveResearchMaterial({ id: crypto.randomUUID(), groupId, kind, title: resolvedSource.split("/").filter(Boolean).pop() || resolvedSource, url: resolvedSource, notes: "从首页来源识别保存", createdAt: Date.now(), updatedAt: Date.now() }, workspace.owner))
  }
  return <div className="mt-4 flex flex-wrap items-center gap-2 rounded-xl bg-[#faf7ff] p-3 text-sm text-[#6b607e]" role="status"><Link2 size={15} className="text-[#7c3aed]" /><span>识别到来源：</span><a href={resolvedSource} target="_blank" rel="noreferrer" className="max-w-[330px] truncate text-[#6d28d9] underline">{resolvedSource}</a><a href={related} className="rounded-lg border border-[#dac9f4] bg-white px-3 py-1.5 text-[#6d28d9]">查找相关研究</a>{workspace.workspace.groups.length > 0 && <><select aria-label="来源保存分组" className="rounded-lg border border-[#dac9d4] bg-white px-2 py-1.5" value={groupId} onChange={event => setGroupId(event.target.value)}><option value="">选择分组</option>{workspace.workspace.groups.map(group => <option key={group.id} value={group.id}>{group.name}</option>)}</select><button type="button" disabled={!groupId || saved} onClick={save} className="rounded-lg border border-[#dac9f4] bg-white px-3 py-1.5 text-[#6d28d9]">{saved ? "已保存" : "保存来源"}</button></>}</div>
}
