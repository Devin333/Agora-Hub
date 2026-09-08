export const researchModules = ["papers", "projects", "community", "reports"] as const
export type ResearchModule = typeof researchModules[number]
export type ResearchMode = "auto" | ResearchModule

export const moduleInfo: Record<ResearchModule, { name: string; command: string; description: string; path: string; examples: string[] }> = {
  papers: { name: "论文研究", command: "找论文", description: "查找论文，阅读原文与证据", path: "/design-demo/papers", examples: ["找 Agent 相关论文", "查找 RAG 评测论文", "了解长上下文研究"] },
  projects: { name: "项目雷达", command: "找项目", description: "发现开源项目，了解实现与活跃度", path: "/projects", examples: ["找可本地运行的开源项目", "找 Agent 开源框架", "看看最近活跃的 RAG 项目"] },
  community: { name: "社区信号", command: "看社区", description: "追踪研究讨论与最新动态", path: "/community", examples: ["了解近期 AI 社区讨论", "看看 Agent 的最新动态", "关注开源社区的讨论"] },
  reports: { name: "研究报告", command: "写报告", description: "整理资料，比较观点，形成报告", path: "/reports", examples: ["整理 Agent 研究报告", "比较 RAG 方法并写报告", "总结长上下文研究进展"] },
}
export const autoExamples = [moduleInfo.papers.examples[0], moduleInfo.projects.examples[0], moduleInfo.community.examples[0]]

export function isResearchMode(value: unknown): value is ResearchMode {
  return value === "auto" || researchModules.includes(value as ResearchModule)
}

// Explicit task objects outrank generic research vocabulary. Unknown intent is a user choice.
export function resolveResearchIntent(question: string, mode: ResearchMode = "auto"): ResearchModule[] {
  if (mode !== "auto") return [mode]
  const text = question.trim().toLowerCase()
  const scores: Record<ResearchModule, number> = {
    papers: /论文|文献|\bpapers?\b|\barxiv\b/.test(text) ? 4 : /研究方法|研究进展|评测|引用|长上下文研究/.test(text) ? 1 : 0,
    projects: /项目|仓库|开源|代码|框架|\b(github|repos?|repositories|projects?)\b/.test(text) ? 4 : /工具|本地运行/.test(text) ? 1 : 0,
    community: /社区|讨论|动态|新闻|行业|\b(community|discussions?|news)\b/.test(text) ? 4 : /趋势/.test(text) ? 1 : 0,
    reports: /报告|\breports?\b/.test(text) ? 4 : /总结|整理|对比|比较|\b(summarize|compare)\b/.test(text) ? 1 : 0,
  }
  if (/写.{0,6}报告|生成.{0,6}报告|整理.{0,12}报告|\b(write|generate|create)\b.{0,20}\breport\b/.test(text)) scores.reports += 5
  for (const [module, pattern] of [
    ["papers", /找.{0,5}论文|查.{0,5}文献|\bfind papers?\b/],
    ["projects", /找.{0,5}项目|找.{0,5}仓库|\bfind projects?\b/],
    ["community", /看.{0,5}讨论|了解.{0,8}社区|\bread news\b/],
  ] as const) if (pattern.test(text)) scores[module] += 2
  const best = Math.max(...Object.values(scores))
  return best === 0 ? [...researchModules] : researchModules.filter(module => scores[module] === best)
}

export function researchSearchQuery(question: string): string {
  // Remove only common instruction framing; retain domain terms and user constraints.
  const value = question.trim().replace(/^(?:请|帮我|我想|想要|帮忙)\s*/g, "")
    .replace(/^(?:找出|查找|找|搜索|检索|看看|了解|关注|整理|总结|比较)\s*/, "")
    .replace(/(?:的)?(?:相关)?(?:论文|文献|开源项目|项目|社区讨论|研究报告|报告)$/, "").trim()
  return value || question.trim()
}

export function researchQuestionHref(module: ResearchModule, question: string): string {
  const params = new URLSearchParams({ question: question.trim(), q: researchSearchQuery(question), entry: "home" })
  if (module === "reports") params.set("compose", "1")
  return `${moduleInfo[module].path}?${params}`
}

export function researchModuleForPath(path: string): ResearchModule | undefined {
  return researchModules.find(module => moduleInfo[module].path === path || (module === "papers" && path === "/papers"))
}

export function safeResearchHref(value: unknown): string | null {
  if (typeof value !== "string" || value.length > 10000 || !value.startsWith("/") || value.startsWith("//") || /[\\\r\n]/.test(value)) return null
  try {
    const url = new URL(value, "https://agora.invalid")
    if (url.origin !== "https://agora.invalid" || !researchModuleForPath(url.pathname)) return null
    return url.pathname + url.search
  } catch { return null }
}
