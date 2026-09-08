"use client"

import { useEffect, useMemo, useRef, useState } from "react"
import * as Dialog from "@radix-ui/react-dialog"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Archive, BookOpen, ClipboardCheck, FileText, Folder, Github, Quote, Search, X, type LucideIcon } from "lucide-react"
import { useRouter } from "next/navigation"
import { moduleInfo, researchModules, type ResearchModule } from "@/lib/research/entry"
import { prepareResearchResume, researchResumeHref } from "@/lib/research/history"
import { groupHref, reportDraftHref } from "@/lib/research/materials"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import styles from "./research-command-palette.module.css"

const moduleIcons: Record<ResearchModule, LucideIcon> = {
  papers: BookOpen,
  projects: Github,
  community: Quote,
  reports: ClipboardCheck,
}
const resultLimit = 8

function matches(query: string, ...values: Array<string | null | undefined>) {
  const tokens = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  if (!tokens.length) return true
  const searchable = values.filter(Boolean).join(" ").toLocaleLowerCase()
  return tokens.every(token => searchable.includes(token))
}

function desktopShortcutEnabled() {
  return typeof window.matchMedia !== "function" || window.matchMedia("(min-width: 1024px)").matches
}

export function ResearchCommandPalette() {
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

  return <Dialog.Root open={open} onOpenChange={next => { setOpen(next); if (!next) setQuery("") }}>
      <Dialog.Trigger asChild>
        <button
          type="button"
          className={styles.trigger}
          onClick={() => { shortcutOrigin.current = null }}
          aria-label="搜索研究和模块"
          aria-keyshortcuts="Control+K Meta+K"
        >
          <Search size={17} aria-hidden="true" />
          <span>搜索研究</span>
          <kbd>Ctrl / Cmd + K</kbd>
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content className={styles.dialog} onCloseAutoFocus={event => {
          const origin = shortcutOrigin.current
          shortcutOrigin.current = null
          if (!origin?.isConnected) return
          event.preventDefault()
          origin.focus({ preventScroll: true })
        }}>
          <Dialog.Title className={styles.visuallyHidden}>全局研究搜索</Dialog.Title>
          <Dialog.Description className={styles.visuallyHidden}>搜索当前账号的研究历史、分组、资料、报告草稿和研究模块。</Dialog.Description>
          <Command className={styles.command} label="搜索研究、资料或模块" shouldFilter={false} loop>
            <CommandInput autoFocus value={query} onValueChange={setQuery} placeholder="搜索研究、资料或模块…" aria-label="搜索研究、资料或模块" />
            <CommandList className={styles.list}>
              {!state.ready && <div className={styles.state} role="status">{state.status === "error" ? "研究工作区暂时无法读取" : "正在读取研究工作区…"}</div>}
              {state.ready && !hasResults && <CommandEmpty className={styles.state}>没有匹配的研究或操作</CommandEmpty>}
              {state.ready && results.modules.length > 0 && <CommandGroup heading="研究模块" className={styles.group}>
                {results.modules.map(module => { const Icon = moduleIcons[module]; return <CommandItem key={module} value={`module:${module}`} className={styles.item} onSelect={() => navigate(moduleInfo[module].path)}>
                  <span className={styles.icon}><Icon size={17} aria-hidden="true" /></span><span className={styles.copy}><strong>{moduleInfo[module].name}</strong><small>{moduleInfo[module].description}</small></span><span className={styles.action}>打开</span>
                </CommandItem> })}
              </CommandGroup>}
              {state.ready && results.visits.length > 0 && <CommandGroup heading="研究历史" className={styles.group}>
                {results.visits.map(visit => { const Icon = moduleIcons[visit.module]; return <CommandItem key={visit.id} value={`visit:${visit.id}`} className={styles.item} onSelect={() => navigate(researchResumeHref(visit), () => prepareResearchResume(visit))}>
                  <span className={styles.icon}>{visit.archivedAt ? <Archive size={17} aria-hidden="true" /> : <Icon size={17} aria-hidden="true" />}</span><span className={styles.copy}><strong>{visit.title}</strong><small>{visit.archivedAt ? `已归档 · ${moduleInfo[visit.module].name}` : visit.activity?.title ?? moduleInfo[visit.module].name}</small></span><span className={styles.action}>继续</span>
                </CommandItem> })}
              </CommandGroup>}
              {state.ready && results.groups.length > 0 && <CommandGroup heading="研究分组" className={styles.group}>
                {results.groups.map(group => <CommandItem key={group.id} value={`group:${group.id}`} className={styles.item} onSelect={() => navigate(groupHref(group.id))}>
                  <span className={styles.icon}><Folder size={17} aria-hidden="true" /></span><span className={styles.copy}><strong>{group.name}</strong><small>{workspace.visits.filter(visit => !visit.deletedAt && visit.groupId === group.id).length} 条研究 · {(workspace.materials ?? []).filter(material => material.groupId === group.id).length} 项资料</small></span><span className={styles.action}>查看</span>
                </CommandItem>)}
              </CommandGroup>}
              {state.ready && results.materials.length > 0 && <CommandGroup heading="研究资料" className={styles.group}>
                {results.materials.map(material => <CommandItem key={material.id} value={`material:${material.id}`} className={styles.item} onSelect={() => navigate(material.readerHref ?? groupHref(material.groupId))}>
                  <span className={styles.icon}><FileText size={17} aria-hidden="true" /></span><span className={styles.copy}><strong>{material.title}</strong><small>{material.notes || material.referenceId || "已保存资料"}</small></span><span className={styles.action}>{material.readerHref ? "阅读" : "查看"}</span>
                </CommandItem>)}
              </CommandGroup>}
              {state.ready && results.drafts.length > 0 && <CommandGroup heading="报告草稿" className={styles.group}>
                {results.drafts.map(draft => <CommandItem key={draft.id} value={`draft:${draft.id}`} className={styles.item} onSelect={() => navigate(reportDraftHref(draft))}>
                  <span className={styles.icon}><ClipboardCheck size={17} aria-hidden="true" /></span><span className={styles.copy}><strong>{draft.title}</strong><small>{draft.materials.length} 项资料 · {draft.scope || "待继续编辑"}</small></span><span className={styles.action}>继续</span>
                </CommandItem>)}
              </CommandGroup>}
            </CommandList>
          </Command>
          <footer className={styles.footer}><span><kbd>↑</kbd><kbd>↓</kbd> 选择</span><span><kbd>Enter</kbd> 打开</span><span><kbd>Esc</kbd> 关闭</span></footer>
          <Dialog.Close className={styles.close} aria-label="关闭全局研究搜索"><X size={17} aria-hidden="true" /></Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
}
