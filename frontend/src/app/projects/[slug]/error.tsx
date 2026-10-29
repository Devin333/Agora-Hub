"use client"

import { ErrorState } from "@/components/common/error-state"
import { ProjectPageBody } from "@/features/projects/components/project-page-layout"

export default function ProjectDetailError({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <ProjectPageBody>
      <ErrorState title="项目详情异常" message={error.message} onRetry={reset} />
    </ProjectPageBody>
  )
}
