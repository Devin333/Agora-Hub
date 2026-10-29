import { RelatedListPanel } from "@/components/papers/shared/related-list-panel"
import { papersCopy, t } from "@/lib/papers/copy"
import { formatWholeNumber, taskName } from "@/lib/papers/format"
import { papersRoutes } from "@/lib/papers/routes"
import type { Locale, Paper, TaskRef } from "@/lib/papers/types"

export function SisterTasksPanel({
  tasks,
  papers,
  locale
}: {
  tasks: TaskRef[]
  papers: Paper[]
  locale: Locale
}) {
  const items = tasks
    .map((task) => ({
      task,
      count: papers.filter(
        (p) => p.isPublished !== false && (p.taskRefs ?? []).some((r) => r.slug === task.slug)
      ).length
    }))
    .filter((item) => item.count > 0)
    .map(({ task, count }) => ({
      id: task.id,
      label: taskName(task, locale),
      href: papersRoutes.taskDetail(task.slug),
      count: formatWholeNumber(count, locale)
    }))

  return <RelatedListPanel title={t(papersCopy.sisterTasks, locale)} items={items} />
}
