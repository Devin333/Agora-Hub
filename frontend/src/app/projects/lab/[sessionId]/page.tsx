import { Suspense } from "react"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { ProjectLabSessionPage } from "@/features/projects/components/projects-detail-pages"

export const dynamic = "force-dynamic"

export default async function ProjectsLabSessionRoute(props: { params: Promise<{ sessionId: string }> }) {
  const params = await props.params;
  return (
    <Suspense fallback={<PageSkeleton />}>
      <ProjectLabSessionPage sessionId={params.sessionId} />
    </Suspense>
  )
}
