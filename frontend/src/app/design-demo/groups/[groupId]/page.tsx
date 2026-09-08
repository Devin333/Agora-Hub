import { ResearchGroupPage } from "@/features/portal/components/research-group-page"

export default function ResearchGroupRoute({ params }: { params: { groupId: string } }) {
  return <ResearchGroupPage groupId={params.groupId === "ungrouped" ? null : params.groupId} />
}
