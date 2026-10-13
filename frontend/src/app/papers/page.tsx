import { PapersPageClient } from "@/app/papers/papers-page-client"
import { PapersDesignDemoPage } from "@/features/portal/components/papers-design-demo-page"
import { getPublishedPapers } from "@/lib/papers/real-data"
import { getPaperCategoryDefinitions } from "@/lib/papers/category-definitions"
import { isLocalDesktopProfile } from "@/lib/deployment-profile"

export const dynamic = "force-dynamic"
export default async function PapersPageRoute() {
  const papers = await getPublishedPapers()
  return isLocalDesktopProfile() ? <PapersDesignDemoPage papers={papers} taxonomy={getPaperCategoryDefinitions()} /> : <PapersPageClient papers={papers} />
}
