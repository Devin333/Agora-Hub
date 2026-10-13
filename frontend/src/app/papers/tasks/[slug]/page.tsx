import { notFound } from "next/navigation"
import { TaskDetailPageClient } from "@/app/papers/tasks/[slug]/task-detail-page-client"
import { getPaperTasksResult, getPublishedPapers } from "@/lib/papers/real-data"
import { decodePaperRouteSlug } from "@/lib/papers/routes"

export function generateStaticParams() {
  return []
}

export default async function PapersTaskDetailPageRoute(props: { params: Promise<{ slug: string }> }) {
  const params = await props.params;
  const slug = decodePaperRouteSlug(params.slug)
  const result = await getPaperTasksResult()
  const task = result.items.find((item) => item.slug === slug && item.paperCount > 0)

  if (!task) {
    notFound()
  }

  return (
    <TaskDetailPageClient
      task={task}
      papers={await getPublishedPapers()}
      fallbackNotice={result.notices[0] ?? null}
    />
  )
}
