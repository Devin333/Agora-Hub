"use client"

import { useState } from "react"
import { CommonBenchmarksPanel } from "@/components/papers/methods/common-benchmarks-panel"
import { RelatedMethodsPanel } from "@/components/papers/methods/related-methods-panel"
import { RelatedTasksPanel } from "@/components/papers/methods/related-tasks-panel"
import { PapersHero } from "@/components/papers/papers-hero"
import { PapersMicrobar } from "@/components/papers/papers-microbar"
import { BenchmarkEvidencePanel, benchmarkPaperMatches } from "@/components/papers/shared/benchmark-evidence-panel"
import { InlineNotice } from "@/components/papers/shared/inline-notice"
import { ImplementationList } from "@/components/papers/shared/implementation-list"
import { PaperDetailDrawer } from "@/components/papers/shared/paper-detail-drawer"
import { PaperStream } from "@/components/papers/shared/paper-stream"
import { translate } from "@/lib/i18n"
import { localizedResearchNotice, papersCopy, t } from "@/lib/papers/copy"
import { methodDescription, methodName } from "@/lib/papers/format"
import { papersRoutes } from "@/lib/papers/routes"
import type { Benchmark, BenchmarkRef, Locale, MethodRef, Paper, PaperMethod, TaskRef } from "@/lib/papers/types"

export function MethodDetailPage({
  method,
  locale,
  papers,
  fallbackNotice
}: {
  method: PaperMethod
  locale: Locale
  papers?: Paper[]
  fallbackNotice?: string | null
}) {
  const fallbackMessage = localizedResearchNotice(fallbackNotice, locale)
  const [notice, setNotice] = useState<string | null>(fallbackMessage)
  const [selectedPaper, setSelectedPaper] = useState<Paper | null>(null)
  const [selectedBenchmark, setSelectedBenchmark] = useState<BenchmarkRef | Benchmark | null>(null)
  const methodPapers = papers?.filter((paper) => paper.isPublished !== false && (paper.methodRefs ?? []).some((methodRef) => methodRef.slug === method.slug)) ?? []
  const commonBenchmarks = evidenceBackedBenchmarks(
    method.commonBenchmarks ?? [],
    methodPapers
  )
  const relatedTasks = relatedTasksFromPapers(methodPapers)
  const relatedMethods = relatedMethodsFromPapers(methodPapers, method.slug)
  const methodStats = deriveMethodDetailStats(methodPapers)

  function previewPaper(paper: Paper) {
    setSelectedPaper(paper)
  }

  function previewBenchmark(benchmark: BenchmarkRef | Benchmark) {
    setSelectedBenchmark(benchmark)
    if (notice && notice !== fallbackMessage) {
      setNotice(null)
    }
  }

  return (
    <div className="min-w-0">
      <PapersMicrobar
        items={[{ label: "Methods", href: papersRoutes.methods }, { label: method.name }]}
        meta={t(papersCopy.methodDetailMeta, locale)}
        locale={locale}
      />
      <PapersHero
        eyebrow="Method"
        title={methodName(method, locale)}
        subtitle={methodDescription(method, locale)}
        stats={[
          { label: t(papersCopy.papers, locale), value: methodStats.paperCount },
          { label: t(papersCopy.tasks, locale), value: methodStats.taskCount },
          { label: t(papersCopy.implementations, locale), value: methodStats.implementationCount }
        ]}
      />
      {notice ? (
        <div className="px-4 pt-4 md:px-6">
          <InlineNotice message={notice} locale={locale} onDismiss={() => setNotice(null)} />
        </div>
      ) : null}
      <div className="grid gap-6 px-4 py-4 md:px-6 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <main className="min-w-0 space-y-6">
          {selectedBenchmark ? (
            <BenchmarkEvidencePanel
              benchmark={selectedBenchmark}
              context={{ type: "method", method }}
              papers={methodPapers}
              locale={locale}
              onClose={() => setSelectedBenchmark(null)}
              onPreviewPaper={previewPaper}
            />
          ) : null}
          <PaperStream papers={methodPapers} locale={locale} title={t(papersCopy.papersUsingMethod, locale)} onPreview={previewPaper} />
          <RelatedTasksPanel tasks={relatedTasks} locale={locale} />
          <ImplementationList papers={methodPapers} locale={locale} title={translate(locale, "papers.reader.projects")} />
        </main>
        <aside className="space-y-6 xl:sticky xl:top-24 xl:self-start">
          <RelatedMethodsPanel methods={relatedMethods} locale={locale} />
          <CommonBenchmarksPanel benchmarks={commonBenchmarks} locale={locale} onSelect={previewBenchmark} />
        </aside>
      </div>
      <PaperDetailDrawer
        paper={selectedPaper}
        locale={locale}
        open={Boolean(selectedPaper)}
        onOpenChange={(open) => {
          if (!open) {
            setSelectedPaper(null)
          }
        }}
      />
    </div>
  )
}

function deriveMethodDetailStats(papers: Paper[]) {
  const taskCount = new Set(papers.flatMap((paper) => (paper.taskRefs ?? []).map((task) => task.slug))).size
  const implementationCount = countImplementationsFromPapers(papers)

  return {
    paperCount: papers.length,
    taskCount,
    implementationCount,
  }
}

function countImplementationsFromPapers(papers: Paper[]) {
  const seen = new Set<string>()
  for (const paper of papers) {
    const implementations = paper.implementations?.length
      ? paper.implementations
      : paper.repoUrl?.startsWith("https://github.com/")
        ? [{ repoUrl: paper.repoUrl }]
        : []
    for (const implementation of implementations) {
      if (implementation.repoUrl) {
        seen.add(implementation.repoUrl)
      }
    }
  }
  return seen.size
}

function evidenceBackedBenchmarks(benchmarks: Array<BenchmarkRef | Benchmark>, papers: Paper[]) {
  return benchmarks.filter((benchmark) => benchmarkPaperMatches(benchmark, papers).length > 0)
}

function relatedTasksFromPapers(papers: Paper[]) {
  return uniqueRefs(papers.flatMap((paper) => paper.taskRefs ?? []))
}

function relatedMethodsFromPapers(papers: Paper[], currentMethodSlug: string) {
  return uniqueRefs((papers.flatMap((paper) => paper.methodRefs ?? [])).filter((method) => method.slug !== currentMethodSlug))
}

function uniqueRefs<T extends TaskRef | MethodRef>(refs: T[]): T[] {
  const seen = new Set<string>()
  const result: T[] = []
  for (const ref of refs) {
    const key = ref.slug || ref.id || ref.name
    if (!key || seen.has(key)) {
      continue
    }
    seen.add(key)
    result.push(ref)
  }
  return result
}
