"use client"

import { RelatedListPanel } from "@/components/papers/shared/related-list-panel"
import { benchmarkCategoryLabel } from "@/lib/papers/categories"
import { papersCopy, t } from "@/lib/papers/copy"
import type { Benchmark, BenchmarkRef, Locale } from "@/lib/papers/types"

export function CommonBenchmarksPanel({
  benchmarks,
  locale,
  onSelect
}: {
  benchmarks: Array<BenchmarkRef | Benchmark>
  locale: Locale
  onSelect: (benchmark: BenchmarkRef | Benchmark) => void
}) {
  const items = benchmarks.map((benchmark) => ({
    id: benchmark.id,
    label: benchmark.name,
    detail: benchmark.category ? benchmarkCategoryLabel(benchmark.category, locale) ?? benchmark.category : undefined,
    onSelect: () => onSelect(benchmark)
  }))

  return <RelatedListPanel title={t(papersCopy.commonBenchmarks, locale)} items={items} />
}
