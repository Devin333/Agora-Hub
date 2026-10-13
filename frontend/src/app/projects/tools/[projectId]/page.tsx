import { Suspense } from "react"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { ProjectToolDetailPage } from "@/features/projects/components/projects-detail-pages"

export const dynamic = "force-dynamic"

export default async function ProjectsToolDetailRoute(props: { params: Promise<{ projectId: string }> }) {
  const params = await props.params;
  return (
    <Suspense fallback={<PageSkeleton />}>
      <ProjectToolDetailPage projectId={params.projectId} />
    </Suspense>
  )
}
