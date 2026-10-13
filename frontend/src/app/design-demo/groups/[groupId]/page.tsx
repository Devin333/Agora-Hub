import { ResearchGroupPage } from "@/features/portal/components/research-group-page"

export default async function ResearchGroupRoute(props: { params: Promise<{ groupId: string }> }) {
  const params = await props.params;
  return <ResearchGroupPage groupId={params.groupId === "ungrouped" ? null : params.groupId} />
}
