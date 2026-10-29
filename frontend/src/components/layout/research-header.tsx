"use client"

import { useEffect, useState } from "react"
import Link from "next/link"
import { useRouter } from "next/navigation"
import { ChevronDown, Menu, Search } from "lucide-react"
import { AccountMenu } from "@/components/auth/account-menu"
import { PapersLanguageToggle } from "@/components/papers/shared/language-toggle"
import { AppearanceMenu } from "@/components/theme/appearance-menu"
import { BrandMark } from "@/components/brand/brand-mark"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetTitle, SheetTrigger } from "@/components/ui/sheet"
import { papersCopy, t } from "@/lib/papers/copy"
import type { Locale } from "@/lib/papers/types"
import { cn } from "@/lib/utils"

export function ResearchHeader({
  locale,
  pathname = "/",
  onLocaleChange
}: {
  locale: Locale
  pathname?: string
  onLocaleChange: (locale: Locale) => void
}) {
  const router = useRouter()
  const [activeGroupId, setActiveGroupId] = useState<string | null>(null)
  const [mobileOpen, setMobileOpen] = useState(false)
  const [searchQuery, setSearchQuery] = useState("")

  useEffect(() => {
    setSearchQuery(currentSearchQuery())
  }, [pathname])

  function handleGroupBlur(event: React.FocusEvent<HTMLDivElement>) {
    const nextFocus = event.relatedTarget
    if (nextFocus instanceof Node && event.currentTarget.contains(nextFocus)) {
      return
    }
    setActiveGroupId(null)
  }

  function submitSearch(event: React.FormEvent<HTMLFormElement>) {
    const query = searchQuery.trim()
    if (!query) {
      event.preventDefault()
      router.push("/papers")
      return
    }
    setMobileOpen(false)
  }

  return (
    <header className="sticky top-0 z-40 border-b border-border bg-background/90 backdrop-blur-xl">
      <div className="mx-auto flex min-h-14 max-w-[1440px] items-center gap-2 px-3 py-2 sm:gap-3 sm:px-6">
        <Link href="/papers" className="flex min-w-0 items-center gap-2 sm:gap-3" aria-label="Agora Hub Research">
          <BrandMark />
          <span className="min-w-0">
            <span className="block truncate text-body1 font-semibold">
              Agora Hub <span className="text-muted-foreground">Research</span>
            </span>
            <span className="hidden truncate text-label text-faint-foreground sm:block">{t(papersCopy.brandSubline, locale)}</span>
          </span>
        </Link>

        <div
          className="relative hidden flex-1 items-center justify-start xl:flex"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              setActiveGroupId(null)
            }
          }}
        >
          <nav className="flex items-center gap-1.5" aria-label="Portal modules">
            {researchHeaderGroups.map((group) => {
              const active = activeGroupId === group.id
              const current = isNavGroupCurrent(pathname, group)
              const dropdownId = `research-header-${group.id}-menu`

              return (
                <div
                  key={group.id}
                  className="relative"
                  onBlur={handleGroupBlur}
                  onMouseEnter={() => setActiveGroupId(group.id)}
                  onMouseLeave={() => setActiveGroupId(null)}
                >
                  <button
                    type="button"
                    className={cn(
                      "inline-flex h-8 items-center gap-1 rounded-full px-3 text-body2 font-medium text-muted-foreground outline-none transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50",
                      (active || current) && "bg-accent text-foreground"
                    )}
                    aria-expanded={active}
                    aria-controls={dropdownId}
                    data-current={current ? "true" : undefined}
                    onClick={() => setActiveGroupId(active ? null : group.id)}
                    onFocus={() => setActiveGroupId(group.id)}
                  >
                    {t(group.label, locale)}
                    <ChevronDown className={cn("size-3.5 transition-transform", active && "rotate-180")} strokeWidth={1.8} />
                  </button>
                  {active ? <ResearchHeaderDropdown id={dropdownId} group={group} locale={locale} pathname={pathname} /> : null}
                </div>
              )
            })}
          </nav>
        </div>

        <form className="relative ml-auto hidden w-[min(26rem,32vw)] md:block" action="/papers" method="get" onSubmit={submitSearch} role="search">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            name="q"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
            className="h-9 rounded-xl bg-card pl-9 dark:bg-input"
            placeholder={t(papersCopy.searchPlaceholder, locale)}
          />
        </form>
        <div className="hidden items-center gap-1 sm:flex">
          <AppearanceMenu />
          <PapersLanguageToggle locale={locale} onLocaleChange={onLocaleChange} />
        </div>
        <AccountMenu />

        <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
          <SheetTrigger asChild>
            <Button type="button" variant="ghost" size="icon" className="xl:hidden" aria-label={t(papersCopy.openNavigation, locale)}>
              <Menu className="size-5" />
            </Button>
          </SheetTrigger>
          <SheetContent className="w-[min(22rem,88vw)] overflow-y-auto">
            <SheetTitle className="sr-only">{t(papersCopy.brand, locale)}</SheetTitle>
            <SheetDescription className="sr-only">{t(papersCopy.brandSubline, locale)}</SheetDescription>
            <div className="mb-6 pr-8">
              <p className="text-sm font-semibold">{t(papersCopy.brand, locale)}</p>
              <p className="text-xs text-muted-foreground">{t(papersCopy.brandSubline, locale)}</p>
            </div>
            <form className="relative mb-4" action="/papers" method="get" onSubmit={submitSearch} role="search">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                name="q"
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
                className="h-9 pl-9"
                placeholder={t(papersCopy.searchPlaceholder, locale)}
              />
            </form>
            <div className="mb-4 flex flex-wrap items-center gap-2 sm:hidden">
              <AppearanceMenu showLabel variant="outline" align="start" />
              <PapersLanguageToggle locale={locale} onLocaleChange={onLocaleChange} />
            </div>
            <nav className="space-y-4" aria-label="Research mobile navigation">
              {researchHeaderGroups.map((group) => (
                <div key={group.id}>
                  <p className="mb-1 px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">
                    {t(group.label, locale)}
                  </p>
                  <div className="space-y-0.5">
                    {group.items.map((item) => {
                      const current = isNavItemCurrent(pathname, item)
                      const itemId = navItemDomId("mobile", group.id, item)
                      const labelId = `${itemId}-label`
                      const descriptionId = `${itemId}-description`

                      return (
                        <SheetClose asChild key={item.href}>
                          <Link
                            href={item.href}
                            className={cn(
                              "block rounded-xl px-2.5 py-2 text-body2 transition-colors hover:bg-sidebar-accent/60",
                              current && "bg-sidebar-accent text-sidebar-accent-foreground"
                            )}
                            aria-current={current ? "page" : undefined}
                            aria-labelledby={labelId}
                            aria-describedby={descriptionId}
                          >
                            <span id={labelId} className="font-medium">{t(item.label, locale)}</span>
                            <span id={descriptionId} className="mt-0.5 line-clamp-2 block text-label text-muted-foreground">
                              {t(item.description, locale)}
                            </span>
                          </Link>
                        </SheetClose>
                      )
                    })}
                  </div>
                </div>
              ))}
            </nav>
          </SheetContent>
        </Sheet>
      </div>
    </header>
  )
}

function currentSearchQuery() {
  if (typeof window === "undefined") {
    return ""
  }
  return new URLSearchParams(window.location.search).get("q") ?? ""
}

type HeaderCopy = Record<Locale, string>

type HeaderNavItem = {
  href: string
  label: HeaderCopy
  description: HeaderCopy
}

type HeaderNavGroup = {
  id: string
  label: HeaderCopy
  items: HeaderNavItem[]
}

function ResearchHeaderDropdown({
  id,
  group,
  locale,
  pathname
}: {
  id: string
  group: HeaderNavGroup
  locale: Locale
  pathname: string
}) {
  return (
    <div id={id} className="absolute left-0 top-full z-50 w-[min(30rem,calc(100vw-2rem))] pt-2">
      <div className="rounded-xl bg-popover p-1.5 text-popover-foreground shadow-md ring-1 ring-foreground/10">
        <div className="grid gap-1">
          {group.items.map((item) => {
            const current = isNavItemCurrent(pathname, item)
            const itemId = navItemDomId("desktop", group.id, item)
            const labelId = `${itemId}-label`
            const descriptionId = `${itemId}-description`

            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  "group rounded-lg px-3 py-2 outline-none transition-colors hover:bg-accent focus-visible:bg-accent",
                  current && "bg-accent"
                )}
                aria-current={current ? "page" : undefined}
                aria-labelledby={labelId}
                aria-describedby={descriptionId}
              >
                <span id={labelId} className="flex items-center justify-between gap-3 text-body2 font-medium text-foreground">
                  {t(item.label, locale)}
                </span>
                <span id={descriptionId} className="mt-0.5 block text-label text-muted-foreground">
                  {t(item.description, locale)}
                </span>
              </Link>
            )
          })}
        </div>
      </div>
    </div>
  )
}

function isNavGroupCurrent(pathname: string, group: HeaderNavGroup) {
  const prefixes = navGroupCurrentPrefixes[group.id]
  if (prefixes) {
    return prefixes.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`))
  }

  return group.items.some((item) => {
    if (item.href.includes("?")) {
      return false
    }
    const path = navItemPath(item)
    return pathname === path || (path !== "/" && pathname.startsWith(`${path}/`))
  })
}

function isNavItemCurrent(pathname: string, item: HeaderNavItem) {
  if (item.href.includes("?")) {
    return false
  }

  const path = navItemPath(item)
  if (pathname === path) {
    return true
  }

  if (path === "/" || path === "/projects" || path === "/papers") {
    return false
  }

  return pathname.startsWith(`${path}/`)
}

function navItemPath(item: HeaderNavItem) {
  return item.href.split("?")[0] || "/"
}

function navItemDomId(scope: string, groupId: string, item: HeaderNavItem) {
  const itemKey = item.href.replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-|-$/g, "") || "root"
  return `research-header-${scope}-${groupId}-${itemKey}`
}

const navGroupCurrentPrefixes: Record<string, string[]> = {
  research: ["/papers"],
  projects: ["/projects"],
  today: ["/news", "/community"],
  trends: ["/topics"],
  reports: ["/reports"]
}

const researchHeaderGroups: HeaderNavGroup[] = [
  {
    id: "research",
    label: papersCopy.papersNav,
    items: [
      {
        href: "/papers",
        label: papersCopy.trendingPapers,
        description: papersCopy.dropdownTrendingDescription
      },
      {
        href: "/papers/tasks",
        label: papersCopy.tasks,
        description: papersCopy.dropdownTasksDescription
      },
      {
        href: "/papers/methods",
        label: papersCopy.methods,
        description: papersCopy.dropdownMethodsDescription
      }
    ]
  },
  {
    id: "projects",
    label: { zh: "项目", en: "Projects" },
    items: [
      {
        href: "/projects",
        label: { zh: "项目总览", en: "Projects" },
        description: { zh: "热门、新星、工具、案例、实验室、合集和关注列表的统一入口。", en: "Unified entry for hot, rising, tools, cases, Lab, collections, and watchlist." }
      },
      {
        href: "/projects/hot",
        label: { zh: "热门项目", en: "Hot Projects" },
        description: { zh: "按外部热度、行为信号、技术相关性和来源可信度排序。", en: "Ranked by external heat, behavior signals, technical relevance, and source trust." }
      },
      {
        href: "/projects/rising",
        label: { zh: "新星项目", en: "Rising Projects" },
        description: { zh: "正在加速增长、更新活跃或质量上升的项目。", en: "Projects with accelerating growth, active updates, or rising quality." }
      },
      {
        href: "/projects/tools",
        label: { zh: "工具", en: "Tools" },
        description: { zh: "按能力、集成方式和部署适配度组织真实项目工具。", en: "Real project tools grouped by capability, integration surface, and deployment fit." }
      },
      {
        href: "/projects/cases",
        label: { zh: "案例", en: "Cases" },
        description: { zh: "由真实项目、能力和公开来源引用沉淀出的模块案例。", en: "Cases derived from real projects, capabilities, and public source references." }
      },
      {
        href: "/projects/lab",
        label: { zh: "实验室", en: "Lab" },
        description: { zh: "从需求画像出发，结合真实项目案例生成方案。", en: "Start from a requirement profile and derive solutions from real project cases." }
      },
      {
        href: "/projects/collections",
        label: { zh: "合集", en: "Collections" },
        description: { zh: "围绕主题生成的真实项目集合，不填充模拟数据。", en: "Topic collections generated from real projects without synthetic filler." }
      },
      {
        href: "/projects/watchlist",
        label: { zh: "关注列表", en: "Watchlist" },
        description: { zh: "持续跟踪你关注的真实项目和状态变化。", en: "Track real projects you care about and their state changes." }
      }
    ]
  },
  {
    id: "today",
    label: { zh: "今日", en: "Today" },
    items: [
      {
        href: "/news",
        label: { zh: "头条", en: "Top Stories" },
        description: { zh: "每日 AI 新闻主线和重点来源。", en: "Daily AI news front page and key sources." }
      },
      {
        href: "/news?source=official",
        label: { zh: "官方更新", en: "Official Updates" },
        description: { zh: "来自官方博客、公告和一手来源的更新。", en: "Updates from official blogs, announcements, and primary sources." }
      },
      {
        href: "/news?topic=product-updates",
        label: { zh: "产品更新", en: "Product Updates" },
        description: { zh: "产品发布、能力变化和采用信号。", en: "Product launches, capability changes, and adoption signals." }
      },
      {
        href: "/community",
        label: { zh: "社区脉搏", en: "Community Pulse" },
        description: { zh: "开发者讨论、争议、反馈和传播路径。", en: "Developer discussion, controversy, feedback, and propagation paths." }
      }
    ]
  },
  {
    id: "trends",
    label: { zh: "趋势", en: "Trends" },
    items: [
      {
        href: "/news?sort=hot",
        label: { zh: "热门", en: "Hot" },
        description: { zh: "当前热度最高的新闻和社区信号。", en: "The hottest current news and community signals." }
      },
      {
        href: "/news?sort=rising",
        label: { zh: "上升", en: "Rising" },
        description: { zh: "正在加速升温的新主题和项目。", en: "New topics and projects gaining momentum." }
      },
      {
        href: "/topics?view=timeline",
        label: { zh: "时间线", en: "Timeline" },
        description: { zh: "按时间追踪主题演化和关键事件。", en: "Track topic evolution and key events over time." }
      },
      {
        href: "/news?sort=controversial",
        label: { zh: "争议", en: "Controversial" },
        description: { zh: "争议、分歧和观点碰撞较强的信号。", en: "Signals with strong disagreement, debate, and controversy." }
      },
      {
        href: "/topics?view=evidence-graph",
        label: { zh: "证据图谱", en: "Evidence Graph" },
        description: { zh: "跨新闻、论文、项目和社区的证据关系。", en: "Evidence relationships across news, papers, projects, and community." }
      },
      {
        href: "/topics?topic=agents",
        label: { zh: "智能体", en: "Agents" },
        description: { zh: "Agent 运行时、工作流和评测方向。", en: "Agent runtimes, workflows, and evaluation directions." }
      },
      {
        href: "/topics?topic=llms",
        label: { zh: "大模型", en: "LLMs" },
        description: { zh: "大模型能力、发布和生态变化。", en: "Model capabilities, releases, and ecosystem changes." }
      },
      {
        href: "/topics?topic=models",
        label: { zh: "模型", en: "Models" },
        description: { zh: "模型架构、基准和应用表现。", en: "Model architectures, benchmarks, and application performance." }
      },
      {
        href: "/topics?view=compare",
        label: { zh: "对比", en: "Compare" },
        description: { zh: "对比主题、来源和趋势信号强度。", en: "Compare topics, sources, and trend signal strength." }
      }
    ]
  },
  {
    id: "reports",
    label: { zh: "报告", en: "Reports" },
    items: [
      {
        href: "/reports?type=daily",
        label: { zh: "日报", en: "Daily" },
        description: { zh: "每日 AI 情报摘要和关键变化。", en: "Daily AI intelligence briefings and key changes." }
      },
      {
        href: "/reports?type=weekly",
        label: { zh: "周报", en: "Weekly" },
        description: { zh: "每周趋势、来源健康和主题复盘。", en: "Weekly trends, source health, and topic reviews." }
      },
      {
        href: "/reports?type=deep-dives",
        label: { zh: "深度分析", en: "Deep Dives" },
        description: { zh: "围绕重点主题生成的长分析报告。", en: "Long-form reports around important topics." }
      },
      {
        href: "/reports?type=briefings",
        label: { zh: "简报", en: "Briefings" },
        description: { zh: "适合快速阅读和分发的简短情报。", en: "Short intelligence briefs for fast reading and sharing." }
      },
      {
        href: "/reports?type=watchlists",
        label: { zh: "关注列表", en: "Watchlists" },
        description: { zh: "持续跟踪的主题、项目和来源集合。", en: "Tracked topics, projects, and source collections." }
      },
      {
        href: "/reports?type=archive",
        label: { zh: "归档", en: "Archive" },
        description: { zh: "历史报告和可追溯情报记录。", en: "Historical reports and traceable intelligence records." }
      }
    ]
  }
]
