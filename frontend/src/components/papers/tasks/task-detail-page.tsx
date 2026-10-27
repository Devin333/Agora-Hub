"use client"

import { useState } from "react"
import { BenchmarkList } from "@/components/papers/tasks/benchmark-list"
import { CommonMethodsPanel } from "@/components/papers/tasks/common-methods-panel"
import { SisterTasksPanel } from "@/components/papers/tasks/sister-tasks-panel"
import { PapersMicrobar } from "@/components/papers/papers-microbar"
import { BenchmarkEvidencePanel } from "@/components/papers/shared/benchmark-evidence-panel"
import { InlineNotice } from "@/components/papers/shared/inline-notice"
import { ImplementationList } from "@/components/papers/shared/implementation-list"
import { PaperDetailDrawer } from "@/components/papers/shared/paper-detail-drawer"
import { PaperStream } from "@/components/papers/shared/paper-stream"
import { translate } from "@/lib/i18n"
import { localizedResearchNotice, papersCopy, t } from "@/lib/papers/copy"
import { formatWholeNumber, taskDescription, taskName } from "@/lib/papers/format"
import { papersRoutes } from "@/lib/papers/routes"
import type { Benchmark, Locale, Paper, PaperTask } from "@/lib/papers/types"

export function TaskDetailPage({
  task,
  locale,
  papers,
  fallbackNotice
}: {
  task: PaperTask
  locale: Locale
  papers?: Paper[]
  fallbackNotice?: string | null
}) {
  const fallbackMessage = localizedResearchNotice(fallbackNotice, locale)
  const [notice, setNotice] = useState<string | null>(fallbackMessage)
  const [selectedPaper, setSelectedPaper] = useState<Paper | null>(null)
  const [selectedBenchmark, setSelectedBenchmark] = useState<Benchmark | null>(null)

  const taskPapers = papers?.filter(
    (p) => p.isPublished !== false && (p.taskRefs ?? []).some((r) => r.slug === task.slug)
  ) ?? []

  const taskBenchmarks = benchmarksForTask(task.slug, taskPapers)
  const taskStats = deriveTaskDetailStats(taskPapers, taskBenchmarks)

  function previewBenchmark(benchmark: Benchmark) {
    setSelectedBenchmark(benchmark)
    if (notice && notice !== fallbackMessage) {
      setNotice(null)
    }
  }

  return (
    <div className="min-w-0">
      <PapersMicrobar
        items={[{ label: "Tasks", href: papersRoutes.tasks }, { label: task.name.toUpperCase() }]}
        meta={t(papersCopy.taskDetailMeta, locale)}
        locale={locale}
      />

      <section className="border-b px-4 py-4 md:px-6">
        <p className="text-label font-medium text-muted-foreground">Task</p>
        <h1 className="mt-1 max-w-5xl break-keep text-balance text-heading2 font-semibold text-foreground">
          {taskName(task, locale)}
        </h1>
        <p className="mt-1 max-w-3xl text-body2 text-muted-foreground">{taskDescription(task, locale)}</p>
        <dl className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-label text-muted-foreground">
          <TaskStat value={formatWholeNumber(taskPapers.length, locale)} label={t(papersCopy.papers, locale)} />
          <TaskStat value={formatWholeNumber(taskStats.benchmarkCount, locale)} label={t(papersCopy.benchmarks, locale)} />
          <TaskStat value={formatWholeNumber(taskStats.methodCount, locale)} label={t(papersCopy.methodsUsed, locale)} />
        </dl>
      </section>

      {notice ? (
        <div className="px-4 pt-4 md:px-6">
          <InlineNotice message={notice} locale={locale} onDismiss={() => setNotice(null)} />
        </div>
      ) : null}

      <div className="grid gap-6 px-4 py-4 md:px-6 xl:grid-cols-[minmax(0,1fr)_18rem] 2xl:grid-cols-[minmax(0,1fr)_19rem]">
        <main className="min-w-0 space-y-6">
          {selectedBenchmark ? (
            <BenchmarkEvidencePanel
              benchmark={selectedBenchmark}
              context={{ type: "task", task }}
              papers={taskPapers}
              locale={locale}
              onClose={() => setSelectedBenchmark(null)}
              onPreviewPaper={(paper) => setSelectedPaper(paper)}
            />
          ) : null}
          <PaperStream
            papers={taskPapers}
            locale={locale}
            title={t(papersCopy.papersUnderTask, locale)}
            onPreview={(p) => setSelectedPaper(p)}
          />
          <BenchmarkList
            benchmarks={taskBenchmarks}
            task={task}
            locale={locale}
            onSelect={previewBenchmark}
          />
          <ImplementationList
            papers={taskPapers}
            locale={locale}
            title={translate(locale, "papers.reader.implementations")}
          />
        </main>

        <aside className="space-y-6 xl:sticky xl:top-24 xl:self-start">
          <SisterTasksPanel tasks={task.sisterTasks} papers={papers ?? []} locale={locale} />
          <CommonMethodsPanel methods={task.commonMethods} papers={papers ?? []} locale={locale} />
        </aside>
      </div>

      <PaperDetailDrawer
        paper={selectedPaper}
        locale={locale}
        open={Boolean(selectedPaper)}
        onOpenChange={(open) => { if (!open) setSelectedPaper(null) }}
      />
    </div>
  )
}

function TaskStat({ value, label }: { value: string; label: string }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt>{label}</dt>
      <dd className="order-first text-body2 font-semibold tabular-nums text-foreground">{value}</dd>
    </div>
  )
}

function deriveTaskDetailStats(papers: Paper[], taskBenchmarks: Benchmark[]) {
  const benchmarkCount = taskBenchmarks.length
    ? taskBenchmarks.filter((benchmark) => (benchmark.entryCount ?? 0) > 0).length
    : countBenchmarksFromPapers(papers)
  const methodCount = new Set(papers.flatMap((paper) => (paper.methodRefs ?? []).map((method) => method.slug))).size

  return {
    benchmarkCount,
    methodCount,
  }
}

function benchmarksForTask(taskSlug: string, papers: Paper[]): Benchmark[] {
  const records = new Map<string, Benchmark>()
  for (const paper of papers) {
    for (const benchmark of paper.benchmarks ?? []) {
      if (benchmark.taskSlug && benchmark.taskSlug !== taskSlug) {
        continue
      }
      const slug = benchmark.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "") || benchmark.id
      const id = benchmark.id || slug
      const existing = records.get(id)
      records.set(id, {
        id,
        slug,
        name: benchmark.name,
        category: benchmark.category,
        taskSlug,
        entryCount: (existing?.entryCount ?? 0) + 1,
        metric: benchmark.metric,
        bestValue: benchmark.value
      })
    }
  }
  return Array.from(records.values()).sort((left, right) => right.entryCount - left.entryCount || left.name.localeCompare(right.name))
}

function countBenchmarksFromPapers(papers: Paper[]) {
  const seen = new Set<string>()
  for (const paper of papers) {
    for (const benchmark of paper.benchmarks ?? []) {
      seen.add(benchmark.id || benchmark.name)
    }
  }
  return seen.size
}
