import { notFound } from "next/navigation"
import { PaperDetailPageClient } from "@/app/papers/[slug]/paper-detail-page-client"
import { getPaperById } from "@/lib/papers/real-data"
import { decodePaperRouteSlug } from "@/lib/papers/routes"

export const dynamic = "force-dynamic"

export default async function PaperDetailPageRoute(props: { params: Promise<{ slug: string }> }) {
  const params = await props.params;
  const paper = await getPaperById(decodePaperRouteSlug(params.slug))
  if (!paper) {
    notFound()
  }

  return <PaperDetailPageClient paper={paper} />
}
