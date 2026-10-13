import { notFound } from "next/navigation"
import { MethodDetailPageClient } from "@/app/papers/methods/[slug]/method-detail-page-client"
import { getPaperMethodsResult, getPublishedPapers } from "@/lib/papers/real-data"
import { decodePaperRouteSlug } from "@/lib/papers/routes"

export function generateStaticParams() {
  return []
}

export default async function PapersMethodDetailPageRoute(props: { params: Promise<{ slug: string }> }) {
  const params = await props.params;
  const slug = decodePaperRouteSlug(params.slug)
  const result = await getPaperMethodsResult()
  const method = result.items.find((item) => item.slug === slug && item.paperCount > 0)

  if (!method) {
    notFound()
  }

  return (
    <MethodDetailPageClient
      method={method}
      papers={await getPublishedPapers()}
      fallbackNotice={result.notices[0] ?? null}
    />
  )
}
