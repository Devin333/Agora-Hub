import { canonicalRefSlug, queryPapers } from "./query"
import type { Paper } from "./types"

export type PaperCategory = { value: string; name: string; nameZh?: string; count: number; code?: string; group?: string; description?: string; descriptionZh?: string }
export type CategoryDefinition = { slug: string; name: string; nameZh?: string; group?: string; description?: string; descriptionZh?: string }
export type PaperCategoryDefinitions = { methods: CategoryDefinition[]; tasks: CategoryDefinition[] }
export type PaperCategoryGroups = { topics: PaperCategory[]; methods: PaperCategory[]; tasks: PaperCategory[] }

const topicNames: Record<string, { nameZh: string; name: string }> = {
  "cs.AI": { nameZh: "人工智能", name: "Artificial intelligence" },
  "cs.LG": { nameZh: "机器学习", name: "Machine learning" },
  "cs.CL": { nameZh: "自然语言处理", name: "Language processing" },
  "cs.CV": { nameZh: "计算机视觉", name: "Computer vision" },
  "cs.RO": { nameZh: "机器人", name: "Robotics" },
  "cs.MA": { nameZh: "多智能体", name: "Multi-agent systems" },
  "cs.IR": { nameZh: "信息检索", name: "Information retrieval" },
  "cs.SE": { nameZh: "软件工程", name: "Software engineering" },
  "cs.HC": { nameZh: "人机交互", name: "Human-computer interaction" },
  "cs.CR": { nameZh: "信息安全", name: "Security" },
  "stat.ML": { nameZh: "统计学习", name: "Statistical learning" }
}

/** Definitions describe categories; associations and counts come only from public papers. */
export function buildPaperCategories(papers: Paper[], definitions?: PaperCategoryDefinitions): PaperCategoryGroups {
  const published = [...new Map(papers.filter(paper => paper.isPublished === true).map(paper => [paper.id, paper])).values()]
  const topicCounts = new Map<string, number>()
  for (const paper of published) for (const tag of new Set(paper.tags)) topicCounts.set(tag, (topicCounts.get(tag) ?? 0) + 1)
  const topics = [...topicCounts].map(([value, count]) => ({ value, count, code: value, ...(topicNames[value] ?? { name: value }) })).sort((a, b) => b.count - a.count || a.value.localeCompare(b.value))
  function refs(kind: "method" | "task", seeds: CategoryDefinition[]) {
    const names = new Map<string, CategoryDefinition>()
    for (const item of [...seeds, ...published.flatMap(paper => kind === "method" ? paper.methodRefs : paper.taskRefs)]) {
      const value = canonicalRefSlug(item.slug.trim(), kind)
      if (value && !names.has(value)) names.set(value, item)
    }
    return [...names].map(([value, item]) => ({ value, name: item.name, nameZh: item.nameZh, group: item.group, description: item.description, descriptionZh: item.descriptionZh, count: queryPapers(published, { [kind]: value }).papers.length }))
      .sort((a, b) => b.count - a.count)
  }
  return { topics, methods: refs("method", definitions?.methods ?? []), tasks: refs("task", definitions?.tasks ?? []) }
}

export function previewCategories<T extends { value: string }>(items: T[], selected: string, expanded: boolean, limit = 5): T[] {
  if (expanded || items.length <= limit) return items
  const first = items.slice(0, limit)
  const active = items.find(item => item.value === selected)
  return active && !first.includes(active) ? [...first.slice(0, limit - 1), active] : first
}
