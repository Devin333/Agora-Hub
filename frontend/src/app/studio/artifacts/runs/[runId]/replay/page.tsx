import { getReplayBundle } from "@/features/studio/artifacts/api/artifact-replay-api"
import { ArtifactRunReplayPage } from "@/features/studio/artifacts/components/artifact-replay-page"

export default async function StudioArtifactReplayPage(props: { params: Promise<{ runId: string }> }) {
  const params = await props.params;
  const { replay, lineage } = await getReplayBundle(params.runId)
  return <ArtifactRunReplayPage replay={replay} lineage={lineage} />
}
