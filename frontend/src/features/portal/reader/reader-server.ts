import { paperDocumentToOpenReader } from "@/lib/paper-reader/open-reader-adapter"
import { getPaperById } from "@/lib/papers/real-data"
import { loadReaderSource } from "./reader-source-server"
import type { ReaderWorkspacePayload } from "./reader-contract"

export async function loadReaderWorkspace(
  paperRef: string,
): Promise<ReaderWorkspacePayload | null> {
  const paper = await getPaperById(paperRef)
  if (!paper || paper.isPublished === false) return null

  const source = await loadReaderSource(paper)
  const sourceHash = source?.payload.document?.sourceHash
  const sourceIdentity = sourceHash
    ? {
        sourceRevision: sourceHash,
        sourceFingerprint: sourceHash.startsWith("sha256:")
          ? sourceHash
          : `sha256:${sourceHash}`,
      }
    : {}
  if (
    source?.payload.document?.status === "compiled" &&
    source.payload.status.status === "compiled" &&
    source.payload.status.gateReport?.passed === true
  ) {
    const adapted = paperDocumentToOpenReader(source.payload)
    const rawSourceUrl = source.payload.document.auxiliary?.sourceUrl
    return {
      paper,
      reader: adapted.reader,
      visualLayer: adapted.visualLayer,
      state: "ready",
      sourceKind: "arxiv-html",
      ...(typeof rawSourceUrl === "string" ? { sourceUrl: rawSourceUrl } : {}),
      aiReady: false,
      ...sourceIdentity,
    }
  }

  // Retrieval/analysis sections can omit appendices and visuals. They are not a full-text fallback.
  return {
    paper,
    reader: null,
    state: source ? "needs_review" : "unavailable",
    reasonCode: source ? "document_quality_pending" : "source_conversion_unavailable",
    aiReady: false,
    ...sourceIdentity,
  }
}
