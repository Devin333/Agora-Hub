import { RunEvidencePage } from "@/features/studio/evidence/components/run-evidence-page"
import { getRunEvidenceDetail } from "@/features/studio/evidence/api/evidence-api"

export default async function StudioEvidenceRunPage(
  props: {
    params: Promise<{ runId: string }>
    searchParams?: Promise<{ reportId?: string }>
  }
) {
  const searchParams = await props.searchParams;
  const params = await props.params;
  const detail = await getRunEvidenceDetail(params.runId, searchParams?.reportId)
  return <RunEvidencePage detail={detail} />
}
