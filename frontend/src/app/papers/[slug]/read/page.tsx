import { notFound } from "next/navigation"
import { PaperDocumentReaderPageClient } from "@/app/papers/[slug]/read/paper-document-reader-page-client"
import { loadPaperDocumentPayload } from "@/lib/paper-reader/server-loader"
import { decodePaperRouteSlug } from "@/lib/papers/routes"
import { safePaperReturnTo } from "@/lib/papers/discovery-navigation"

export const dynamic = "force-dynamic"

export default async function PaperDocumentReadRoute({ params, searchParams }: { params: { slug: string }; searchParams?: { returnTo?: string | string[] } }) {
  const payload = await loadPaperDocumentPayload(decodePaperRouteSlug(params.slug))
  if (!payload) {
    notFound()
  }

  return <PaperDocumentReaderPageClient payload={payload} backHref={safePaperReturnTo(typeof searchParams?.returnTo === "string" ? searchParams.returnTo : null)} />
}
