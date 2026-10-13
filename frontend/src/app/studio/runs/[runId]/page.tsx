import { RunCenterDetailPage } from "@/features/studio/runs/components/run-center-detail-page";
import { fetchRunCenterDetail } from "@/features/studio/runs/api/run-center-api";
import { adaptRunDetail } from "@/features/studio/runs/lib/run-center-adapter";

export default async function StudioRunDetailPage(props: { params: Promise<{ runId: string }> }) {
  const params = await props.params;
  const detail = adaptRunDetail(params.runId, await fetchRunCenterDetail(params.runId));
  return <RunCenterDetailPage runId={params.runId} detail={detail} />;
}
