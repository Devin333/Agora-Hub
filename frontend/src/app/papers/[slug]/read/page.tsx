import { notFound } from "next/navigation"
import { PaperDocumentReaderPageClient } from "@/app/papers/[slug]/read/paper-document-reader-page-client"
import { DesktopReaderPage } from "@/features/portal/reader/desktop-reader-page"
import { loadPaperDocumentPayload } from "@/lib/paper-reader/server-loader"
import { loadReaderWorkspace } from "@/features/portal/reader/reader-server"
import { decodePaperRouteSlug } from "@/lib/papers/routes"
import { safePaperReturnTo } from "@/lib/papers/discovery-navigation"
import { isHostedProfile } from "@/lib/deployment-profile"

export const dynamic = "force-dynamic"
export default async function PaperDocumentReadRoute(
  props: { params: Promise<{ slug: string }>; searchParams?: Promise<{ returnTo?: string | string[] }> }
) {
  const searchParams = await props.searchParams;
  const params = await props.params;
  const paperId = decodePaperRouteSlug(params.slug)
  const backHref = safePaperReturnTo(typeof searchParams?.returnTo === "string" ? searchParams.returnTo : null)
  if (isHostedProfile()) return <DesktopReaderPage paperId={paperId} fallback={await loadReaderWorkspace(paperId)} backHref={backHref} />
  const payload = await loadPaperDocumentPayload(paperId)
  if (!payload) notFound()
  return <PaperDocumentReaderPageClient payload={payload} backHref={backHref} />
}
