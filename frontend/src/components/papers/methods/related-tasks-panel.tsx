import { RelatedListPanel } from "@/components/papers/shared/related-list-panel"
import { papersCopy, t } from "@/lib/papers/copy"
import { taskName } from "@/lib/papers/format"
import { papersRoutes } from "@/lib/papers/routes"
import type { Locale, TaskRef } from "@/lib/papers/types"

export function RelatedTasksPanel({ tasks, locale }: { tasks: TaskRef[]; locale: Locale }) {
  const items = tasks.map((task) => ({
    id: task.id,
    label: taskName(task, locale),
    href: papersRoutes.taskDetail(task.slug)
  }))

  return <RelatedListPanel title={t(papersCopy.relatedTasks, locale)} items={items} />
}
