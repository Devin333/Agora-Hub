"use client"

import { ArrowRight, Layers3 } from "lucide-react"
import { benchmarkCategoryLabel } from "@/lib/papers/categories"
import { papersCopy, t } from "@/lib/papers/copy"
import { taskName } from "@/lib/papers/format"
import type { Benchmark, Locale, MethodRef, PaperTask, TaskRef } from "@/lib/papers/types"

export function BenchmarkList({
  benchmarks,
  task,
  locale,
  onSelect
}: {
  benchmarks: Benchmark[]
  task: PaperTask
  locale: Locale
  onSelect: (benchmark: Benchmark) => void
}) {
  const visibleItems = benchmarks.length ? benchmarkItems(benchmarks) : branchItems(task)
  if (!visibleItems.length) return null

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-1">
        <h2 className="text-body1 font-semibold text-foreground">
          {benchmarks.length ? t(papersCopy.benchmarksTitle, locale) : t(papersCopy.taskBranches, locale)}
        </h2>
        <p className="max-w-sm text-label text-muted-foreground sm:text-right">
          {benchmarks.length ? t(papersCopy.benchmarkEntryHelp, locale) : t(papersCopy.taskBranchHelp, locale)}
        </p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {visibleItems.map((item) => (
          <button
            key={item.id}
            type="button"
            className="group flex flex-col rounded-2xl border bg-card p-4 text-left outline-none transition-colors hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-ring/50"
            onClick={() => onSelect(item.benchmark)}
          >
            <span className="flex items-center gap-3">
              <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground">
                <Layers3 className="size-4" strokeWidth={1.8} aria-hidden="true" />
              </span>
              <span className="min-w-0 flex-1 truncate text-body1 font-semibold text-foreground">{item.name}</span>
            </span>
            {item.benchmark.category ? (
              <span className="mt-3 inline-flex h-5 w-fit items-center rounded-full bg-muted px-2 text-caption font-medium uppercase tracking-wider text-muted-foreground">
                {benchmarkCategoryLabel(item.benchmark.category, locale) ?? item.benchmark.category}
              </span>
            ) : null}
            <span className="mt-auto flex items-center justify-between gap-3 pt-3 text-label text-muted-foreground">
              <span className="tabular-nums">{item.meta}</span>
              <ArrowRight
                className="size-4 text-faint-foreground transition-transform group-hover:translate-x-0.5"
                strokeWidth={1.8}
                aria-hidden="true"
              />
            </span>
          </button>
        ))}
      </div>
    </section>
  )
}

type BenchmarkCardItem = {
  id: string
  name: string
  meta: string
  benchmark: Benchmark
}

function benchmarkItems(benchmarks: Benchmark[]): BenchmarkCardItem[] {
  return benchmarks.map((benchmark) => ({
    id: benchmark.id,
    name: benchmark.name,
    meta: `${benchmark.entryCount} entries`,
    benchmark
  }))
}

function branchItems(task: PaperTask): BenchmarkCardItem[] {
  const taskBranches = task.sisterTasks.slice(0, 3).map((taskRef) => taskBranchItem(task, taskRef))
  const methodBranches = task.commonMethods.slice(0, 3).map((methodRef) => methodBranchItem(task, methodRef))
  return [...taskBranches, ...methodBranches]
}

function taskBranchItem(task: PaperTask, taskRef: TaskRef): BenchmarkCardItem {
  return {
    id: `${task.id}-${taskRef.id}`,
    name: taskName(taskRef, "en"),
    meta: "task branch",
    benchmark: {
      id: `${task.id}-${taskRef.id}`,
      slug: taskRef.slug,
      name: taskName(taskRef, "en"),
      taskSlug: task.slug,
      entryCount: task.paperCount
    }
  }
}

function methodBranchItem(task: PaperTask, methodRef: MethodRef): BenchmarkCardItem {
  return {
    id: `${task.id}-${methodRef.id}`,
    name: methodRef.name,
    meta: "method branch",
    benchmark: {
      id: `${task.id}-${methodRef.id}`,
      slug: methodRef.slug,
      name: methodRef.name,
      taskSlug: task.slug,
      entryCount: task.methodCount
    }
  }
}
