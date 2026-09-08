import { PapersDesignDemoPage } from "@/features/portal/components/papers-design-demo-page"
import { getPublishedPapers } from "@/lib/papers/real-data"
import { getPaperCategoryDefinitions } from "@/lib/papers/category-definitions"

export const dynamic = "force-dynamic"
export const metadata = { title: "论文研究 | Agora AI" }

export default async function PapersDesignDemoRoute() {
  const papers = await getPublishedPapers()
  return <PapersDesignDemoPage papers={papers} taxonomy={getPaperCategoryDefinitions()} />
}
