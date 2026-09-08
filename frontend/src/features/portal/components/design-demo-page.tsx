"use client"

import { useEffect, useRef, useState, useTransition, type FormEvent } from "react"
import { useRouter } from "next/navigation"
import { ArrowRight, BookOpen, Check, ChevronDown, ClipboardCheck, Github, LoaderCircle, Quote, Sparkles, WandSparkles } from "lucide-react"
import { PortalAccountControl } from "@/components/auth/portal-account-control"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { autoExamples, moduleInfo, researchModules, researchQuestionHref, resolveResearchIntent, type ResearchMode, type ResearchModule } from "@/lib/research/entry"
import { prepareResearchResume, type ResearchVisit } from "@/lib/research/history"
import { useResearchDraft } from "@/lib/research/use-research-draft"
import { ResearchSidebar } from "./research-sidebar"

const icons = { auto: Sparkles, papers: BookOpen, projects: Github, community: Quote, reports: ClipboardCheck }
const focusStyle = "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#8b5cf6] focus-visible:ring-offset-2"

export function DesignDemoPage() {
  const router = useRouter()
  const { query, setQuery, mode, setMode, ready } = useResearchDraft()
  const inputRef = useRef<HTMLInputElement>(null)
  const composing = useRef(false)
  const [choices, setChoices] = useState<ResearchModule[]>([])
  const [collapsed, setCollapsed] = useState(false)
  const [destination, setDestination] = useState<ResearchModule | null>(null)
  const [error, setError] = useState("")
  const [pending, startTransition] = useTransition()
  const ModeIcon = icons[mode]
  const examples = mode === "auto" ? autoExamples : moduleInfo[mode].examples

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
  function navigate(module: ResearchModule, href = researchQuestionHref(module, query, crypto.randomUUID()), restoring = false) {
    setError(""); setChoices([]); setDestination(module)
    startTransition(() => {
      try { if (restoring) router.push(href, { scroll: false }); else router.push(href) }
      catch { setDestination(null); setError("暂时无法打开页面，请重试。你的问题已保留。") }
    })
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!query.trim() || composing.current || destination || pending) return
    const candidates = resolveResearchIntent(query, mode)
    if (candidates.length === 1) navigate(candidates[0])
    else { setChoices(candidates); setError("") }
  }
  function resume(visit: ResearchVisit) { prepareResearchResume(visit); navigate(visit.module, visit.href, true) }
  function newResearch() { setQuery(""); setMode("auto"); setChoices([]); setError(""); setDestination(null); focusInput() }
  function toggleSidebar() { setCollapsed(value => { const next = !value; try { localStorage.setItem("agora-research-sidebar-collapsed", String(next)) } catch { /* Layout remains usable. */ } return next }) }
  const busy = Boolean(!ready || destination || pending)

  return <div className="h-screen overflow-hidden bg-[#fbf8ff] font-papers-research text-[#211a3c] [--header-height:73px]">
    <header className="h-[var(--header-height)] border-b border-[#eee8f5] bg-white">
      <nav className="mx-auto flex h-full max-w-[1280px] items-center justify-between px-10" aria-label="主导航">
        <a href="/design-demo" className={`flex items-center gap-2.5 rounded-lg ${focusStyle}`}><span className="flex size-9 items-center justify-center rounded-[10px] bg-[#7c3aed] text-white shadow-[0_5px_12px_rgba(124,58,237,0.22)]"><WandSparkles className="size-[18px]" /></span><span className="text-[19px] font-bold text-[#2b2148]">Agora<span className="text-[#7c3aed]">AI</span></span></a>
        <div className="flex items-center gap-8 text-base text-[#6d6286]"><a href="#modules" className={`rounded-md hover:text-[#6735d3] ${focusStyle}`}>研究模块</a></div>
        <div className="flex items-center gap-3"><PortalAccountControl /><button type="button" onClick={focusInput} className={`inline-flex h-11 items-center rounded-xl bg-[#7c3aed] px-5 text-base font-semibold text-white shadow-[0_5px_14px_rgba(124,58,237,0.25)] hover:bg-[#6d28d9] ${focusStyle}`}>开始研究</button></div>
      </nav>
    </header>
    <div className="flex h-[calc(100svh-var(--header-height))]">
    <ResearchSidebar busy={busy} collapsed={collapsed} onToggle={toggleSidebar} onNew={newResearch} onResume={resume} />
    <div className="min-w-0 flex-1 overflow-y-auto" aria-label="研究工作区">
    {/* The composer is centered within the right workspace, accounting for the header. */}
    <main id="workspace" className="grid h-[calc(100svh-var(--header-height)-var(--header-height))] min-h-[520px] grid-rows-[minmax(0,1fr)_auto_minmax(0,1fr)]">
      <h1 className="mx-auto w-[calc(100%-80px)] max-w-[900px] self-start pt-[clamp(5rem,12vh,9rem)] text-center font-sans text-[56px] font-bold leading-[1.2]"><span className="text-[#35274f]">Ask.</span>{" "}<span className="text-[#7c3aed]">Discover.</span></h1>
      <section aria-label="研究提问框" className="relative mx-auto w-[calc(100%-80px)] max-w-[960px] rounded-3xl border border-[#e7dff1] bg-white p-8 shadow-[0_18px_45px_rgba(86,58,127,0.13)]">
        <form onSubmit={submit} aria-busy={busy} className="flex h-[72px] items-center gap-3 rounded-[24px] border border-[#ded4ec] bg-[#fefeff] p-2.5 pl-3 shadow-[0_3px_12px_rgba(86,58,127,0.06)] focus-within:border-[#a783ec] focus-within:ring-2 focus-within:ring-[#f0e9ff]">
          <DropdownMenu modal={false}><DropdownMenuTrigger disabled={busy} aria-label="选择研究模式" className={`inline-flex h-11 shrink-0 items-center gap-2 rounded-xl px-3 text-base font-semibold text-[#5b4c76] hover:bg-[#f3edff] ${focusStyle}`}><span className="flex size-8 items-center justify-center rounded-lg bg-[#f0e9ff] text-[#7c3aed]"><ModeIcon size={18} /></span>{mode === "auto" ? "自动" : moduleInfo[mode].command}<ChevronDown size={16} /></DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-80 rounded-2xl border-[#e4d9ef] bg-white p-2 text-[#3f3158] shadow-[0_14px_36px_rgba(60,41,93,0.16)]">
              {(["auto", ...researchModules] as ResearchMode[]).map(item => { const Icon = icons[item]; return <DropdownMenuItem key={item} onSelect={() => { setMode(item); setChoices([]); setError("") }} className="flex cursor-pointer items-center gap-3 rounded-xl px-3 py-3 focus:bg-[#f3edff]"><Icon className="size-5 shrink-0 text-[#7c3aed]" /><span className="flex-1"><span className="block text-base font-semibold">{item === "auto" ? "自动" : moduleInfo[item].command}</span><span className="mt-1 block text-sm text-[#766885]">{item === "auto" ? "根据问题选择合适的模块" : moduleInfo[item].description}</span></span>{mode === item && <Check className="size-4 text-[#7c3aed]" />}</DropdownMenuItem> })}
            </DropdownMenuContent>
          </DropdownMenu>
          <input ref={inputRef} value={query} maxLength={2000} onChange={event => { setQuery(event.target.value); setChoices([]); setError("") }} onCompositionStart={() => { composing.current = true }} onCompositionEnd={() => { composing.current = false }} onKeyDown={event => { if (event.key === "Enter" && (event.nativeEvent.isComposing || event.keyCode === 229 || composing.current)) event.preventDefault() }} aria-label="向 Agora AI 提问" placeholder="输入你想研究的问题…" className="min-w-0 flex-1 bg-transparent px-1 text-lg text-[#3a304f] outline-none placeholder:text-[#93879f]" />
          <button disabled={!query.trim() || busy} type="submit" aria-label="发送问题" className={`flex size-12 shrink-0 items-center justify-center rounded-full bg-[#7c3aed] text-white transition-colors hover:bg-[#6d28d9] disabled:cursor-not-allowed disabled:bg-[#c3abea] ${focusStyle}`}>{busy ? <LoaderCircle className="size-5 animate-spin motion-reduce:animate-none" /> : <ArrowRight size={21} />}</button>
        </form>
        <div className="mt-5 flex flex-wrap items-center gap-2.5 px-1"><span className="mr-1 text-[15px] text-[#756982]">试试：</span>{examples.map(prompt => <button key={prompt} disabled={busy} type="button" onClick={() => { setQuery(prompt); setChoices([]); setError(""); inputRef.current?.focus() }} className={`rounded-xl border border-[#e7dff1] bg-white px-3.5 py-2.5 text-left text-[15px] leading-5 text-[#6b607e] transition-colors hover:border-[#b99beb] hover:bg-[#faf7ff] disabled:opacity-60 ${focusStyle}`}>{prompt}</button>)}</div>
        {choices.length > 0 && <div className="mt-5 flex flex-wrap items-center gap-2 rounded-xl bg-[#faf7ff] p-3 text-[15px]" role="group" aria-label="确认研究方向"><span className="mr-1 text-[#6b607e]">你更想了解哪一类？</span>{choices.map(module => <button key={module} type="button" onClick={() => navigate(module)} className={`rounded-lg border border-[#dac9f4] bg-white px-3 py-2 text-[#6d28d9] hover:bg-[#f0e9ff] ${focusStyle}`}>{moduleInfo[module].command}</button>)}</div>}
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
    </div>
    </div>
  </div>
}
