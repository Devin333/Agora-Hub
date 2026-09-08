import type { Paper, PaperReaderPayload } from "@/lib/papers/types"
import type { OpenReaderVisualLayer } from "@/components/papers/open-reader/open-reader-types"

export type ReaderWorkspacePayload = {
  paper: Paper
  reader: PaperReaderPayload | null
  state: "ready" | "unavailable" | "needs_review"
  reasonCode?: string
  visualLayer?: OpenReaderVisualLayer
  sourceUrl?: string
  sourceKind?: "arxiv-html"
  aiReady?: boolean
}

export function readerUnavailableMessage(code?: string) {
  if (code === "research_configuration_invalid") return "这篇论文的全文转换服务尚未配置完成。你可以先记录问题，或查看论文来源。"
  if (code?.includes("quality") || code === "document_quality_pending") return "全文转换结果还未通过完整性校验，请稍后重试。"
  return "暂时无法取得这篇论文的完整转换结果。请重新获取全文，或查看论文来源。"
}
