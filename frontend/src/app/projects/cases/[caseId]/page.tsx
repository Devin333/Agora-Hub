import { Suspense } from "react"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { ProjectCaseDetailPage } from "@/features/projects/components/projects-detail-pages"

export const dynamic = "force-dynamic"

export default async function ProjectsCaseDetailRoute(props: { params: Promise<{ caseId: string }> }) {
  const params = await props.params;
  return (
    <Suspense fallback={<PageSkeleton />}>
      <ProjectCaseDetailPage caseId={params.caseId} />
    </Suspense>
  )
}
