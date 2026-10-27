import { ProjectPageBody } from "@/features/projects/components/project-page-layout"
import { LabWorkspaceSkeleton } from "@/features/projects/components/projects-product-page"

export default function ProjectsLabLoading() {
  return (
    <ProjectPageBody>
      <LabWorkspaceSkeleton />
    </ProjectPageBody>
  )
}
