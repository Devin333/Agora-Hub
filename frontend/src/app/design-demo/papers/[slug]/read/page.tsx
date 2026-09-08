import { notFound } from "next/navigation"
import { loadReaderWorkspace } from "@/features/portal/reader/reader-server"
import { decodePaperRouteSlug } from "@/lib/papers/routes"
import { safePaperReturnTo } from "@/lib/papers/discovery-navigation"
import { PaperReaderWorkspace } from "@/features/portal/reader/paper-reader-workspace"

export const dynamic = "force-dynamic"
export const metadata = { title: "论文阅读 | Agora AI" }

export default async function ReaderWorkspaceRoute({ params, searchParams }: {
  params: { slug: string }; searchParams?: { returnTo?: string | string[] }
}) {
  const payload = await loadReaderWorkspace(decodePaperRouteSlug(params.slug))
  if (!payload) notFound()
  const requestedReturn = typeof searchParams?.returnTo === "string" ? searchParams.returnTo : "/design-demo/papers"
  return <PaperReaderWorkspace key={payload.paper.id} payload={payload} backHref={safePaperReturnTo(requestedReturn)} />
}
