import { PageSkeleton } from "@/components/common/loading-skeleton"
import { ProjectPageBody } from "@/features/projects/components/project-page-layout"

export default function ProjectsLoading() {
  return (
    <ProjectPageBody>
      <PageSkeleton />
    </ProjectPageBody>
  )
}
