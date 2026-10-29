import type { Metadata } from "next"
import { PaperAgentLab } from "@/features/paper-agent-lab/paper-agent-lab"
import { parsePreviewState } from "@/features/paper-agent-lab/preview-state"

export const metadata: Metadata = {
  title: "Agora Papers · 论文工作台设计预览",
  description: "隔离的论文阅读工作台设计预览，使用示例文献且不连接研究服务。",
}

type PageSearchParams = Promise<Record<string, string | string[] | undefined>>

export default async function PaperAgentLabPage({ searchParams }: { searchParams: PageSearchParams }) {
  const resolvedSearchParams = await searchParams
  const query = new URLSearchParams()

  // `?theme=` is not preview state: the client applies it to the app-wide appearance preference.
  for (const key of ["paper", "view", "tab", "focus", "assistant"] as const) {
    const value = resolvedSearchParams[key]
    if (Array.isArray(value)) {
      for (const item of value) query.append(key, item)
    } else if (value !== undefined) {
      query.set(key, value)
    }
  }

  return <PaperAgentLab initialState={parsePreviewState(query.toString())} />
}
