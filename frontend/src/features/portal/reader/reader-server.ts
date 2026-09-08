import { getPaperById } from "@/lib/papers/real-data"
import type { ReaderWorkspacePayload } from "./reader-contract"
import { loadReaderSource } from "./reader-source-server"
import { paperDocumentToOpenReader } from "@/lib/paper-reader/open-reader-adapter"

export async function loadReaderWorkspace(paperRef: string): Promise<ReaderWorkspacePayload | null> {
  const paper = await getPaperById(paperRef)
  if (!paper || paper.isPublished === false) return null
  const source = await loadReaderSource(paper)
  if (source?.payload.document?.status === "compiled" && source.payload.status.status === "compiled" && source.payload.status.gateReport?.passed === true) {
    const adapted = paperDocumentToOpenReader(source.payload)
    return { paper, reader: adapted.reader, visualLayer: adapted.visualLayer, state: "ready", sourceKind: "arxiv-html", sourceUrl: source.payload.document?.auxiliary?.sourceUrl as string, aiReady: false }
  }
  // Retrieval/analysis sections can omit appendices and visuals. They are not a full-text fallback.
  return { paper, reader: null, state: source ? "needs_review" : "unavailable", reasonCode: source ? "document_quality_pending" : "source_conversion_unavailable", aiReady: false }
}
