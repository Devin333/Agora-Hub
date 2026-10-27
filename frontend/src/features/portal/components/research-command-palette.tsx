"use client"

import { useEffect, useMemo, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Archive, BookOpen, ClipboardCheck, FileText, Folder, Github, Quote, Search, X, type LucideIcon } from "lucide-react"
import { useRouter } from "next/navigation"
import { moduleInfo, researchModules, type ResearchModule } from "@/lib/research/entry"
import { prepareResearchResume, researchResumeHref } from "@/lib/research/history"
import { groupHref, reportDraftHref } from "@/lib/research/materials"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import { cn } from "@/lib/utils"

const moduleIcons: Record<ResearchModule, LucideIcon> = {
  papers: BookOpen,
  projects: Github,
  community: Quote,
  reports: ClipboardCheck,
}
const resultLimit = 8
const itemClass = "items-center gap-3 rounded-xl px-2.5 py-2.5"
const kbdClass = "inline-flex h-5 min-w-5 items-center justify-center rounded-sm bg-muted px-1 font-sans text-xs font-medium text-muted-foreground"

/** One palette row in the rakazo command list: avatar, title, one-line subtitle and the action it runs. */
function PaletteRow({ icon: Icon, title, subtitle, action }: { icon: LucideIcon; title: string; subtitle: string; action: string }) {
  return <>
    <span className="grid size-8 shrink-0 place-items-center rounded-full bg-muted text-muted-foreground ring-1 ring-foreground/10"><Icon size={15} strokeWidth={1.8} aria-hidden="true" /></span>
    <span className="min-w-0 flex-1">
      <span className="block truncate font-medium text-foreground">{title}</span>
      <span className="mt-0.5 block truncate text-[13px] text-muted-foreground">{subtitle}</span>
    </span>
    <kbd className={kbdClass}>{action}</kbd>
  </>
}

function matches(query: string, ...values: Array<string | null | undefined>) {
  const tokens = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  if (!tokens.length) return true
  const searchable = values.filter(Boolean).join(" ").toLocaleLowerCase()
  return tokens.every(token => searchable.includes(token))
}

function desktopShortcutEnabled() {
  return typeof window.matchMedia !== "function" || window.matchMedia("(min-width: 1024px)").matches
}

type ResearchCommandPaletteProps = {
  inline?: boolean
  showTrigger?: boolean
}

export function ResearchCommandPalette({
  inline = false,
  showTrigger = true,
}: ResearchCommandPaletteProps) {
  const router = useRouter()
  const state = useOwnedResearchWorkspace()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState("")
  const shortcutOrigin = useRef<HTMLElement | null>(null)
  const workspace = state.workspace

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key.toLocaleLowerCase() !== "k" || (!event.metaKey && !event.ctrlKey) || !desktopShortcutEnabled()) return
      event.preventDefault()
      setOpen(current => {
        if (!current) shortcutOrigin.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
        return !current
      })
    }
    document.addEventListener("keydown", onKeyDown)
    return () => document.removeEventListener("keydown", onKeyDown)
  }, [])

  useEffect(() => {
    setOpen(false)
    setQuery("")
  }, [state.owner])

  const results = useMemo(() => {
    const modules = researchModules.filter(module => matches(query, moduleInfo[module].name, moduleInfo[module].command, moduleInfo[module].description))
    const groups = workspace.groups.filter(group => matches(query, group.name, "分组"))
    const visits = workspace.visits
      .filter(visit => !visit.deletedAt && matches(query, visit.title, visit.question, moduleInfo[visit.module].name, visit.activity?.title, visit.activity?.kind === "reader" ? visit.activity.sectionTitle : undefined))
      .sort((left, right) => right.updatedAt - left.updatedAt)
    const materials = (workspace.materials ?? [])
      .filter(material => matches(query, material.title, material.notes, material.referenceId, material.url, "资料"))
      .sort((left, right) => right.updatedAt - left.updatedAt)
    const drafts = (workspace.reportDrafts ?? [])
      .filter(draft => matches(query, draft.title, draft.question, draft.scope, draft.notes, "报告 草稿"))
      .sort((left, right) => right.updatedAt - left.updatedAt)
    return {
      modules: modules.slice(0, resultLimit),
      visits: visits.slice(0, resultLimit),
      groups: groups.slice(0, resultLimit),
      materials: materials.slice(0, resultLimit),
      drafts: drafts.slice(0, resultLimit),
    }
  }, [query, workspace])

  function navigate(href: string, beforeNavigate?: () => void) {
    beforeNavigate?.()
    setOpen(false)
    setQuery("")
    router.push(href)
  }

  const hasResults = Object.values(results).some(items => items.length > 0)

  return <Dialog open={open} onOpenChange={next => { setOpen(next); if (!next) setQuery("") }}>
    {showTrigger && <DialogTrigger asChild>
      <Button
        type="button"
        variant="floating"
        size="lg"
        className={cn("gap-2 pe-1.5 ps-3.5", inline ? "flex" : "fixed bottom-6 right-6 z-40 hidden shadow-md lg:inline-flex")}
        onClick={() => { shortcutOrigin.current = null }}
        aria-label="搜索研究和模块"
        aria-keyshortcuts="Control+K Meta+K"
      >
        <Search size={15} strokeWidth={1.8} aria-hidden="true" />
        <span>搜索研究</span>
        <kbd className={cn(kbdClass, "bg-background")}>Ctrl / Cmd + K</kbd>
      </Button>
    </DialogTrigger>}
    <DialogContent
      showCloseButton={false}
      className="top-1/3 translate-y-0 gap-0 overflow-hidden rounded-xl p-0 sm:max-w-xl"
      onCloseAutoFocus={event => {
        const origin = shortcutOrigin.current
        shortcutOrigin.current = null
        if (!origin?.isConnected) return
        event.preventDefault()
        origin.focus({ preventScroll: true })
      }}
    >
      <DialogHeader className="sr-only">
        <DialogTitle>全局研究搜索</DialogTitle>
        <DialogDescription>搜索当前账号的研究历史、分组、资料、报告草稿和研究模块。</DialogDescription>
      </DialogHeader>
      <Command className="rounded-xl bg-popover" label="搜索研究、资料或模块" shouldFilter={false} loop>
        <div className="pe-10"><CommandInput autoFocus value={query} onValueChange={setQuery} placeholder="搜索研究、资料或模块…" aria-label="搜索研究、资料或模块" /></div>
        <CommandList className="max-h-80 scroll-py-1 p-1">
          {!state.ready && <div className="py-10 text-center text-body2 text-muted-foreground" role="status">{state.status === "error" ? "研究工作区暂时无法读取" : "正在读取研究工作区…"}</div>}
          {state.ready && !hasResults && <CommandEmpty className="py-10">没有匹配的研究或操作</CommandEmpty>}
          {state.ready && results.modules.length > 0 && <CommandGroup heading="研究模块">
            {results.modules.map(module => <CommandItem key={module} value={`module:${module}`} className={itemClass} onSelect={() => navigate(moduleInfo[module].path)}>
              <PaletteRow icon={moduleIcons[module]} title={moduleInfo[module].name} subtitle={moduleInfo[module].description} action="打开" />
            </CommandItem>)}
          </CommandGroup>}
          {state.ready && results.visits.length > 0 && <CommandGroup heading="研究历史">
            {results.visits.map(visit => <CommandItem key={visit.id} value={`visit:${visit.id}`} className={itemClass} onSelect={() => navigate(researchResumeHref(visit), () => prepareResearchResume(visit))}>
              <PaletteRow icon={visit.archivedAt ? Archive : moduleIcons[visit.module]} title={visit.title} subtitle={visit.archivedAt ? `已归档 · ${moduleInfo[visit.module].name}` : visit.activity?.title ?? moduleInfo[visit.module].name} action="继续" />
            </CommandItem>)}
          </CommandGroup>}
          {state.ready && results.groups.length > 0 && <CommandGroup heading="研究分组">
            {results.groups.map(group => <CommandItem key={group.id} value={`group:${group.id}`} className={itemClass} onSelect={() => navigate(groupHref(group.id))}>
              <PaletteRow icon={Folder} title={group.name} subtitle={`${workspace.visits.filter(visit => !visit.deletedAt && visit.groupId === group.id).length} 条研究 · ${(workspace.materials ?? []).filter(material => material.groupId === group.id).length} 项资料`} action="查看" />
            </CommandItem>)}
          </CommandGroup>}
          {state.ready && results.materials.length > 0 && <CommandGroup heading="研究资料">
            {results.materials.map(material => <CommandItem key={material.id} value={`material:${material.id}`} className={itemClass} onSelect={() => navigate(material.readerHref ?? groupHref(material.groupId))}>
              <PaletteRow icon={FileText} title={material.title} subtitle={material.notes || material.referenceId || "已保存资料"} action={material.readerHref ? "阅读" : "查看"} />
            </CommandItem>)}
          </CommandGroup>}
          {state.ready && results.drafts.length > 0 && <CommandGroup heading="报告草稿">
            {results.drafts.map(draft => <CommandItem key={draft.id} value={`draft:${draft.id}`} className={itemClass} onSelect={() => navigate(reportDraftHref(draft))}>
              <PaletteRow icon={ClipboardCheck} title={draft.title} subtitle={`${draft.materials.length} 项资料 · ${draft.scope || "待继续编辑"}`} action="继续" />
            </CommandItem>)}
          </CommandGroup>}
        </CommandList>
      </Command>
      <footer className="flex items-center gap-4 border-t border-border bg-muted/50 px-4 py-2.5 text-label text-muted-foreground">
        <span className="inline-flex items-center gap-1"><kbd className={kbdClass}>↑</kbd><kbd className={kbdClass}>↓</kbd>选择</span>
        <span className="inline-flex items-center gap-1"><kbd className={kbdClass}>Enter</kbd>打开</span>
        <span className="inline-flex items-center gap-1"><kbd className={kbdClass}>Esc</kbd>关闭</span>
      </footer>
      <DialogClose asChild><Button variant="ghost" size="icon-sm" className="absolute right-2 top-2" aria-label="关闭全局研究搜索"><X /></Button></DialogClose>
    </DialogContent>
  </Dialog>
}
