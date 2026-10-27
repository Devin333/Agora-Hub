"use client"

import { ErrorState } from "@/components/common/error-state"
import { ProjectPageBody } from "@/features/projects/components/project-page-layout"

export default function ProjectsError({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <ProjectPageBody>
      <ErrorState title="项目雷达异常" message={error.message} onRetry={reset} />
    </ProjectPageBody>
  )
}
