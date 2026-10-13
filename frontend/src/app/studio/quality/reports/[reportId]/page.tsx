import { getQualityDetail } from "@/features/studio/quality/api/quality-gate-api"
import { QualityReportDetailPage } from "@/features/studio/quality/components/quality-gate-page"
import { requestQualityReportReview } from "@/app/studio/quality/reports/[reportId]/actions"

export default async function StudioQualityReportPage(props: { params: Promise<{ reportId: string }> }) {
  const params = await props.params;
  const detail = await getQualityDetail(params.reportId)
  return <QualityReportDetailPage detail={detail} requestReviewAction={requestQualityReportReview} />
}
