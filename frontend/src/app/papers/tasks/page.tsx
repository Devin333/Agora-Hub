import { PaperTaxonomyDirectory } from "@/features/portal/components/paper-taxonomy-directory"
import { getPaperResearchDataset } from "@/lib/papers/real-data"
import { getPaperCategoryDefinitions } from "@/lib/papers/category-definitions"

export const dynamic = "force-dynamic"
export const metadata = { title: "研究任务 | Agora AI" }

export default async function PapersTasksPageRoute() {
  const data = await getPaperResearchDataset()
  return <PaperTaxonomyDirectory kind="task" papers={data.papers.filter(paper => paper.taskRefs.length > 0)} definitions={getPaperCategoryDefinitions(data.methods, data.tasks)} source={data.source} />
}
