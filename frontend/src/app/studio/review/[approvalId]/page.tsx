import { notFound } from "next/navigation"
import { HumanReviewDetailPage } from "@/features/studio/review/components/human-review-detail-page"
import { getReviewItemDetail } from "@/features/studio/review/lib/human-review-adapter"

export default async function StudioReviewDetailRoute(props: { params: Promise<{ approvalId: string }> }) {
  const params = await props.params;
  const detail = await getReviewItemDetail(params.approvalId)
  if (!detail) notFound()
  return <HumanReviewDetailPage detail={detail} />
}
