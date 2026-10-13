import { Suspense } from "react"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { ProjectV1DetailPage } from "@/features/projects/components/projects-detail-pages"

export const dynamic = "force-dynamic"

export default async function ProjectDetailPage(props: { params: Promise<{ slug: string }> }) {
  const params = await props.params;
  return (
    <Suspense fallback={<PageSkeleton />}>
      <ProjectV1DetailPage projectId={params.slug} />
    </Suspense>
  )
}
