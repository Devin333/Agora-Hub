import { translate } from "@/lib/i18n"
import type { TranslationKey } from "@/lib/i18n/translations"
import type { Locale, PaperPeriod, PaperSort } from "@/lib/papers/types"

export type Localized<T = string> = Record<Locale, T>

function copy(key: TranslationKey): Localized {
  return {
    zh: translate("zh", key),
    en: translate("en", key)
  }
}

export const papersCopy = {
  brand: copy("papers.brand"),
  brandSubline: copy("papers.brandSubline"),
  papersNav: copy("papers.papersNav"),
  trendingPapers: copy("papers.trendingPapers"),
  tasks: copy("papers.tasks"),
  methods: copy("papers.methods"),
  searchPlaceholder: copy("papers.searchPlaceholder"),
  openNavigation: copy("papers.openNavigation"),
  themeLight: copy("preference.theme.switchLight"),
  themeDark: copy("preference.theme.switchDark"),
  lightMode: copy("preference.theme.light"),
  darkMode: copy("preference.theme.dark"),
  languageToggle: copy("preference.language.switch"),
  dropdownTrendingDescription: {
    zh: "论文流：PDF 首页、摘要、标签、代码与引用指标。",
    en: "Paper stream with PDF first pages, abstracts, tags, code, and citation signals."
  },
  dropdownTasksDescription: {
    zh: "Papers 分支下的研究任务和领域目录。",
    en: "Research tasks and domain catalog inside Papers."
  },
  dropdownMethodsDescription: {
    zh: "论文相关方法、模型结构与技术路线。",
    en: "Methods, model structures, and technical routes tied to papers."
  },
  frontendView: copy("papers.frontendView"),
  taskBranch: copy("papers.taskBranch"),
  methodBranch: copy("papers.methodBranch"),
  taskDetailMeta: {
    zh: "公开任务详情 · Benchmarks + Papers",
    en: "Public task detail · benchmarks + papers"
  },
  methodDetailMeta: {
    zh: "公开方法详情 · Papers + Tasks",
    en: "Public method detail · papers + tasks"
  },
  researchPapers: copy("papers.researchPapers"),
  researchSubtitle: copy("papers.researchSubtitle"),
  tasksSubtitle: copy("papers.tasksSubtitle"),
  methodsSubtitle: copy("papers.methodsSubtitle"),
  loadingTasks: copy("papers.loadingTasks"),
  loadingMethods: copy("papers.loadingMethods"),
  searchAction: copy("papers.searchAction"),
  apiUnavailableCache: copy("papers.apiUnavailableCache"),
  updatingPapers: copy("papers.updatingPapers"),
  loadMorePapers: copy("papers.loadMorePapers"),
  taskBranches: copy("papers.taskBranches"),
  benchmarkEntryHelp: copy("papers.benchmarkEntryHelp"),
  taskBranchHelp: copy("papers.taskBranchHelp"),
  taskApiFallback: copy("papers.taskApiFallback"),
  methodApiFallback: copy("papers.methodApiFallback"),
  papers: copy("papers.papers"),
  repositories: copy("papers.repositories"),
  benchmarks: copy("papers.benchmarks"),
  implementations: copy("papers.implementations"),
  methodsUsed: copy("papers.methodsUsed"),
  topDomains: copy("papers.topDomains"),
  trendingDomains: copy("papers.trendingDomains"),
  projectDomains: copy("papers.projectDomains"),
  contentPanel: copy("papers.contentPanel"),
  benchmarksTitle: copy("papers.benchmarksTitle"),
  papersUnderTask: copy("papers.papersUnderTask"),
  papersUsingMethod: copy("papers.papersUsingMethod"),
  relatedTasks: copy("papers.relatedTasks"),
  sisterTasks: copy("papers.sisterTasks"),
  commonMethods: copy("papers.commonMethods"),
  relatedMethods: copy("papers.relatedMethods"),
  commonBenchmarks: copy("papers.commonBenchmarks"),
  noPapers: copy("papers.noPapers"),
  openPaper: copy("papers.openPaper"),
  openCode: copy("papers.openCode"),
  githubStars: copy("papers.githubStars"),
  githubMomentum: copy("papers.githubMomentum"),
  citations: copy("papers.citations"),
  paperRecord: copy("papers.paperRecord"),
  paperPreview: {
    zh: "当前版本使用论文详情抽屉预览；独立 Reader 页面会在有数据时打开。",
    en: "Paper detail opens in the drawer; the dedicated Reader route opens when data is available."
  },
  benchmarkPreview: {
    zh: "当前版本 Benchmark 详情先作为占位操作处理。",
    en: "Benchmark detail is a placeholder action for this version."
  },
  dismiss: copy("papers.dismiss")
} satisfies Record<string, Localized>

export const sortLabels: Record<PaperSort, Localized> = {
  relevance: { zh: "相关度", en: "Relevance" },
  trending: copy("papers.sort.trending"),
  newest: copy("papers.sort.newest"),
  most_cited: copy("papers.sort.most_cited")
}

export const periodLabels: Record<PaperPeriod, Localized> = {
  daily: copy("papers.period.daily"),
  weekly: copy("papers.period.weekly"),
  monthly: copy("papers.period.monthly"),
  all: copy("papers.period.all")
}

export const taskGroupLabels: Record<string, Localized> = {
  general: copy("papers.group.general"),
  vision: copy("papers.group.vision"),
  video: copy("papers.group.video"),
  language: copy("papers.group.language"),
  audio: copy("papers.group.audio"),
  robotics: copy("papers.group.robotics"),
  infra: copy("papers.group.infra")
}

const researchNoticeCopy: Record<string, Localized> = {
  "Using verified cached paper data while the live paper index refreshes.": {
    zh: "正在使用已缓存的真实论文数据。",
    en: "Using verified cached paper data."
  },
  "Using the latest verified Paper Radar artifacts while cached paper data refreshes.": {
    zh: "正在使用最新的真实 Paper Radar 论文产物。",
    en: "Using the latest verified Paper Radar paper artifacts."
  },
  "No verified public paper data is available yet.": {
    zh: "当前暂无可展示的真实公开论文数据。",
    en: "No verified public paper data is available yet."
  },
  "No public papers are available.": {
    zh: "当前暂无可展示的真实公开论文数据。",
    en: "No verified public paper data is available yet."
  },
  "Task taxonomy is built from verified paper references while the curated directory refreshes.": {
    zh: "任务目录正在使用真实论文引用聚合结果。",
    en: "Task taxonomy is built from verified paper references."
  },
  "Method taxonomy is built from verified paper references while the curated directory refreshes.": {
    zh: "方法目录正在使用真实论文引用聚合结果。",
    en: "Method taxonomy is built from verified paper references."
  },
  "Backend paper API returned no public papers; showing tracked paper cache.": {
    zh: "正在使用已缓存的真实论文数据。",
    en: "Using verified cached paper data."
  },
  "Backend paper API is unavailable; showing tracked paper cache.": {
    zh: "正在使用已缓存的真实论文数据。",
    en: "Using verified cached paper data."
  },
  "No public backend or tracked cache papers are available; showing latest public Paper Radar artifacts.": {
    zh: "正在使用最新的真实 Paper Radar 论文产物。",
    en: "Using the latest verified Paper Radar paper artifacts."
  },
  "Backend paper API and tracked cache are unavailable; showing latest Paper Radar artifacts.": {
    zh: "正在使用最新的真实 Paper Radar 论文产物。",
    en: "Using the latest verified Paper Radar paper artifacts."
  },
  "No public papers are available from backend, tracked cache, or artifacts.": {
    zh: "当前暂无可展示的真实公开论文数据。",
    en: "No verified public paper data is available yet."
  },
  "No backend, tracked cache, or artifact papers are available.": {
    zh: "当前暂无可展示的真实公开论文数据。",
    en: "No verified public paper data is available yet."
  },
  "Paper task API is unavailable; showing taxonomy derived from real paper references.": {
    zh: "任务目录正在使用真实论文引用聚合结果。",
    en: "Task taxonomy is built from verified paper references."
  },
  "Paper task API is unavailable; showing taxonomy with real paper-derived counts.": {
    zh: "任务目录正在使用真实论文引用聚合结果。",
    en: "Task taxonomy is built from verified paper references."
  },
  "Paper method API is unavailable; showing taxonomy derived from real paper references.": {
    zh: "方法目录正在使用真实论文引用聚合结果。",
    en: "Method taxonomy is built from verified paper references."
  },
  "Paper method API is unavailable; showing taxonomy with real paper-derived counts.": {
    zh: "方法目录正在使用真实论文引用聚合结果。",
    en: "Method taxonomy is built from verified paper references."
  }
}

export function localizedResearchNotice(value: string | null | undefined, locale: Locale) {
  if (!value) {
    return null
  }
  return researchNoticeCopy[value]?.[locale] ?? value
}

export function t(value: Localized | undefined, locale: Locale, fallback = "") {
  if (!value) {
    return fallback
  }
  return value[locale] ?? value.en ?? fallback
}
