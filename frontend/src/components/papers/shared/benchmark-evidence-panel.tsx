"use client"

import { X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { methodName, taskName } from "@/lib/papers/format"
import type { Benchmark, BenchmarkRef, Locale, Paper, PaperBenchmarkResult, PaperMethod, PaperTask } from "@/lib/papers/types"

type BenchmarkEvidenceContext =
  | { type: "task"; task: PaperTask }
  | { type: "method"; method: PaperMethod }

export function BenchmarkEvidencePanel({
  benchmark,
  context,
  papers,
  locale,
  onClose,
  onPreviewPaper
}: {
  benchmark: BenchmarkRef | Benchmark
  context: BenchmarkEvidenceContext
  papers: Paper[]
  locale: Locale
  onClose: () => void
  onPreviewPaper: (paper: Paper) => void
}) {
  const matches = benchmarkPaperMatches(benchmark, papers)
  const category = benchmark.category ? benchmark.category : undefined
  const metric = "metric" in benchmark ? benchmark.metric : undefined
  const bestValue = "bestValue" in benchmark ? benchmark.bestValue : undefined
  const entryCount = matches.length

  return (
    <section className="rounded-2xl border bg-card p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">
            {locale === "zh" ? "评测证据" : "Benchmark Evidence"}
          </p>
          <h2 className="mt-1 text-heading3 font-semibold text-foreground">{benchmark.name}</h2>
          <p className="mt-1 max-w-3xl text-body2 text-muted-foreground">
            {contextDescription(context, locale)}
          </p>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          className="shrink-0 rounded-full text-muted-foreground"
          aria-label={locale === "zh" ? "关闭评测证据" : "Close benchmark evidence"}
          onClick={onClose}
        >
          <X className="size-4" strokeWidth={1.8} aria-hidden="true" />
        </Button>
      </div>
      <dl className="mt-4 grid gap-2 sm:grid-cols-3">
        <BenchmarkFact label={locale === "zh" ? "记录条目" : "Entries"} value={entryCount || matches.length || 0} />
        <BenchmarkFact label={locale === "zh" ? "指标" : "Metric"} value={metric ?? "Not recorded"} />
        <BenchmarkFact label={locale === "zh" ? "最佳值" : "Best Value"} value={bestValue ?? "Not recorded"} />
      </dl>
      {category ? (
        <p className="mt-3 text-label text-muted-foreground">
          {locale === "zh" ? "类别" : "Category"}: {category}
        </p>
      ) : null}
      <div className="mt-5 space-y-2">
        <h3 className="px-1 text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">
          {locale === "zh" ? "相关论文" : "Relevant Papers"}
        </h3>
        {matches.length ? (
          <ul className="divide-y overflow-hidden rounded-xl border">
            {matches.map((match) => (
              <li key={match.paper.id}>
                <button
                  type="button"
                  className="block w-full px-4 py-3 text-left text-body2 text-foreground outline-none transition-colors hover:bg-accent/40 focus-visible:bg-accent/40"
                  onClick={() => onPreviewPaper(match.paper)}
                >
                  <span className="block font-medium">{match.paper.title}</span>
                  <span className="mt-0.5 block text-label text-muted-foreground">{match.reason}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="px-1 text-body2 text-muted-foreground">
            {emptyMessage(context, locale)}
          </p>
        )}
      </div>
    </section>
  )
}

function BenchmarkFact({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-xl bg-muted px-3 py-2.5">
      <dt className="text-label text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-body1 font-semibold tabular-nums text-foreground">{value}</dd>
    </div>
  )
}

function contextDescription(context: BenchmarkEvidenceContext, locale: Locale) {
  if (context.type === "task") {
    return locale === "zh"
      ? `这里展示它和 ${taskName(context.task, locale)} 的真实关联：来自当前任务下论文记录的 benchmark 字段，以及论文标题或摘要中的直接提及。`
      : `This shows why it is tied to ${taskName(context.task, locale)} using recorded benchmark fields on papers in this task, plus direct title or abstract mentions.`
  }
  return locale === "zh"
    ? `这里展示它和 ${methodName(context.method, locale)} 的真实关联：来自当前方法下论文记录的 benchmark 字段，以及论文标题或摘要中的直接提及。`
    : `This shows why it is tied to ${methodName(context.method, locale)} using recorded benchmark fields on papers in this method, plus direct title or abstract mentions.`
}

function emptyMessage(context: BenchmarkEvidenceContext, locale: Locale) {
  if (context.type === "task") {
    return locale === "zh"
      ? "当前任务下没有论文记录这个 benchmark 的结构化结果或直接文本提及。"
      : "No paper under this task records a structured result or direct text mention for this benchmark yet."
  }
  return locale === "zh"
    ? "当前方法下没有论文记录这个 benchmark 的结构化结果或直接文本提及。"
    : "No paper under this method records a structured result or direct text mention for this benchmark yet."
}

export function benchmarkPaperMatches(benchmark: BenchmarkRef | Benchmark, papers: Paper[]) {
  return papers.flatMap((paper) => {
    const result = (paper.benchmarks ?? []).find((item) => benchmarkResultMatches(item, benchmark))
    if (result) {
      const metric = result.metric ? `${result.metric}: ` : ""
      const value = result.value != null ? `${result.value}` : "recorded result"
      return [{ paper, reason: `${metric}${value}` }]
    }
    const text = `${paper.title} ${paper.abstractSnippet} ${paper.tags.join(" ")}`.toLocaleLowerCase()
    const terms = [benchmark.slug, benchmark.name].filter(Boolean).map((item) => item.toLocaleLowerCase())
    const matchedTerm = terms.find((term) => text.includes(term))
    return matchedTerm ? [{ paper, reason: `Mentioned in paper metadata: ${matchedTerm}` }] : []
  })
}

function benchmarkResultMatches(result: PaperBenchmarkResult, benchmark: BenchmarkRef | Benchmark) {
  const sameSlug = result.id === benchmark.id || slugifyBenchmarkName(result.name) === benchmark.slug
  const sameName = result.name.toLocaleLowerCase() === benchmark.name.toLocaleLowerCase()
  return sameSlug || sameName
}

function slugifyBenchmarkName(value: string) {
  return value.toLocaleLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")
}
