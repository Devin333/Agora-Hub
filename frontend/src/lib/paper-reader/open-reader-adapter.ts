import type { OpenReaderVisualLayer } from "@/components/papers/open-reader/open-reader-types"
import type { PaperReaderPayload, PaperSection } from "@/lib/papers/types"
import type { PaperBlock, PaperDocument, PaperDocumentResponse, PaperInlineSpan, PaperReference } from "@/lib/paper-reader/types"

type MutableSection = PaperSection & {
  textParts: string[]
  blockIds: string[]
  blockSources: unknown[]
  sourceOrders: number[]
  blockInlineSpans: unknown[]
  sectionNumber?: string
  sourceOrder?: number
}

export function paperDocumentToOpenReader(payload: PaperDocumentResponse): {
  reader: PaperReaderPayload
  visualLayer: OpenReaderVisualLayer
} {
  const document = payload.document
  const manifest = payload.manifest
  if (!document || !manifest || payload.status.status !== "compiled") {
    return {
      reader: emptyReaderPayload(payload),
      visualLayer: { blocks: [], outline: [], references: [] },
    }
  }

  const assetsById = new Map(manifest.assets.map((asset) => [asset.assetId, asset]))
  const sections = new Map<string, MutableSection>()
  let currentSectionId = `${payload.paper.id}:compiled-body`
  let currentSectionTitle = document.title || payload.paper.title
  let currentSectionType: PaperSection["sectionType"] = "unknown"
  let currentSectionLevel = 1
  let currentSectionNumber: string | undefined
  let currentSectionSourceOrder = 0
  const visualBlocks: OpenReaderVisualLayer["blocks"] = []

  for (let order = 0; order < document.blocks.length; order += 1) {
    const block = document.blocks[order]
    if (block.type === "heading") {
      const heading = normalizeHeading(block)
      currentSectionId = block.sectionId || block.id
      currentSectionTitle = heading.title || document.title || payload.paper.title
      currentSectionType = sectionTypeFromTitle(currentSectionTitle)
      currentSectionLevel = normalizeLevel(block.level)
      currentSectionNumber = heading.sectionNumber
      currentSectionSourceOrder = order
      ensureSection(
        sections,
        payload.paper.id,
        currentSectionId,
        currentSectionTitle,
        currentSectionLevel,
        currentSectionType,
        block.pageNumber,
        currentSectionNumber,
        currentSectionSourceOrder,
      )
      continue
    }

    if (block.type === "paragraph") {
      const section = ensureSection(
        sections,
        payload.paper.id,
        block.sectionId || currentSectionId,
        currentSectionTitle,
        currentSectionLevel,
        currentSectionType,
        block.pageNumber,
        currentSectionNumber,
        currentSectionSourceOrder,
      )
      if (block.text?.trim()) {
        section.textParts.push(block.text.trim())
        section.blockIds.push(block.id)
        section.blockSources.push(block.source ?? null)
        section.sourceOrders.push(order)
        section.blockInlineSpans.push(readInlineSpans(block))
        section.pageStart = minNumber(section.pageStart, block.pageNumber)
        section.pageEnd = maxNumber(section.pageEnd, block.pageNumber)
      }
      continue
    }

    if (isVisualBlock(block)) {
      const section = ensureSection(
        sections,
        payload.paper.id,
        block.sectionId || currentSectionId,
        currentSectionTitle,
        currentSectionLevel,
        currentSectionType,
        block.pageNumber,
        currentSectionNumber,
        currentSectionSourceOrder,
      )
      section.pageStart = minNumber(section.pageStart, block.pageNumber)
      section.pageEnd = maxNumber(section.pageEnd, block.pageNumber)
      visualBlocks.push({
        id: block.id,
        paperId: block.paperId,
        sectionId: section.id,
        sectionTitle: section.title,
        sectionType: section.sectionType,
        sectionLevel: section.level,
        sectionNumber: section.sectionNumber,
        order,
        block,
        asset: block.assetId ? assetsById.get(block.assetId) : undefined,
        assets: Array.isArray(block.metadata?.assetIds) ? block.metadata.assetIds.map((id) => assetsById.get(String(id))).filter((asset): asset is NonNullable<typeof asset> => Boolean(asset)) : undefined,
        source: block.source,
      })
    }
  }

  const readerSections = Array.from(sections.values())
    .map(finalizeSection)
    .filter((section) => section.textExcerpt.trim() || visualBlocks.some((block) => block.sectionId === section.id))

  return {
    reader: {
      paper: payload.paper,
      sections: readerSections,
      aiSummary: payload.ai?.summary ?? payload.paper.aiSummary ?? null,
      readerNotes: [],
      relatedPapers: [],
      relatedProjects: [],
      relatedNews: [],
      quality: {
        paperId: payload.paper.id,
        pdfAvailable: Boolean(payload.paper.pdfUrl || payload.paper.paperUrl || payload.paper.arxivUrl),
        textExtracted: readerSections.some((section) => section.textExcerpt.trim()),
        summaryAvailable: Boolean(payload.ai?.summary ?? payload.paper.aiSummary),
        implementationVerified: Boolean(payload.paper.implementations?.length),
        benchmarkVerified: Boolean(payload.paper.benchmarks?.length),
        evidenceCoverage: payload.paper.evidenceRefs?.length ? 1 : 0,
        lastUpdatedAt: payload.status.updatedAt,
      },
    },
    visualLayer: {
      blocks: visualBlocks,
      outline: readOutline(document, sections),
      references: readReferences(document.auxiliary),
    },
  }
}

function emptyReaderPayload(payload: PaperDocumentResponse): PaperReaderPayload {
  return {
    paper: payload.paper,
    sections: [],
    aiSummary: payload.ai?.summary ?? payload.paper.aiSummary ?? null,
    readerNotes: [],
    relatedPapers: [],
    relatedProjects: [],
    relatedNews: [],
    quality: {
      paperId: payload.paper.id,
      pdfAvailable: Boolean(payload.paper.pdfUrl || payload.paper.paperUrl || payload.paper.arxivUrl),
      textExtracted: false,
      summaryAvailable: Boolean(payload.ai?.summary ?? payload.paper.aiSummary),
      implementationVerified: Boolean(payload.paper.implementations?.length),
      benchmarkVerified: Boolean(payload.paper.benchmarks?.length),
      evidenceCoverage: payload.paper.evidenceRefs?.length ? 1 : 0,
      lastUpdatedAt: payload.status.updatedAt,
    },
  }
}

function ensureSection(
  sections: Map<string, MutableSection>,
  paperId: string,
  id: string,
  title: string,
  level: number,
  sectionType: PaperSection["sectionType"],
  pageNumber?: number,
  sectionNumber?: string,
  sourceOrder?: number,
) {
  const existing = sections.get(id)
  if (existing) {
    existing.level = normalizeLevel(existing.level || level)
    if (sectionNumber && !existing.sectionNumber) existing.sectionNumber = sectionNumber
    if (sourceOrder !== undefined) existing.sourceOrder = minNumber(existing.sourceOrder, sourceOrder)
    existing.pageStart = minNumber(existing.pageStart, pageNumber)
    existing.pageEnd = maxNumber(existing.pageEnd, pageNumber)
    return existing
  }
  const section: MutableSection = {
    id,
    paperId,
    title: title || "Paper Body",
    level: normalizeLevel(level),
    pageStart: pageNumber,
    pageEnd: pageNumber,
    textExcerpt: "",
    sectionType,
    sectionNumber,
    sourceOrder,
    textParts: [],
    blockIds: [],
    blockSources: [],
    sourceOrders: [],
    blockInlineSpans: [],
  }
  sections.set(id, section)
  return section
}

function finalizeSection(section: MutableSection): PaperSection {
  const metadata: Record<string, unknown> = {
    blockIds: section.blockIds,
    blockSources: section.blockSources,
    sourceOrders: section.sourceOrders,
    blockInlineSpans: section.blockInlineSpans,
  }
  if (section.sectionNumber) metadata.sectionNumber = section.sectionNumber
  if (section.sourceOrder !== undefined) metadata.sectionSourceOrder = section.sourceOrder
  return {
    id: section.id,
    paperId: section.paperId,
    title: section.title,
    level: section.level,
    pageStart: section.pageStart,
    pageEnd: section.pageEnd,
    textExcerpt: section.textParts.join("\n\n"),
    sectionType: section.sectionType,
    metadata,
  }
}

function isVisualBlock(block: PaperBlock) {
  return block.type === "figure" || block.type === "table" || block.type === "equation"
}

function sectionTypeFromTitle(title: string): PaperSection["sectionType"] {
  const normalized = title.toLowerCase()
  if (normalized.includes("abstract")) return "abstract"
  if (normalized.includes("introduction")) return "introduction"
  if (normalized.includes("related")) return "related_work"
  if (normalized.includes("method") || normalized.includes("approach")) return "method"
  if (normalized.includes("experiment") || normalized.includes("evaluation")) return "experiment"
  if (normalized.includes("result")) return "result"
  if (normalized.includes("limitation")) return "limitation"
  if (normalized.includes("conclusion")) return "conclusion"
  if (normalized.includes("appendix")) return "appendix"
  return "unknown"
}

function normalizeHeading(block: PaperBlock) {
  const rawTitle = block.text?.trim() || "Paper Body"
  const metadataNumber = optionalText(block.metadata?.sectionNumber)
  const parsed = splitLeadingSectionNumber(rawTitle)
  return {
    title: parsed && (!metadataNumber || parsed.sectionNumber === metadataNumber) ? parsed.title : rawTitle,
    sectionNumber: metadataNumber ?? parsed?.sectionNumber,
  }
}

function splitLeadingSectionNumber(value: string) {
  const match = /^(\d+(?:\.\d+)*)(?:[.)])?\s+(.+)$/.exec(value.trim())
  if (!match) return null
  return {
    sectionNumber: match[1],
    title: match[2].trim(),
  }
}

function normalizeLevel(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.min(6, Math.max(1, Math.round(value))) : 1
}

function optionalText(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : undefined
}

function readOutline(document: PaperDocument, sections: Map<string, MutableSection>): OpenReaderVisualLayer["outline"] {
  const sectionById = new Map(sections)
  const headingSectionIdByBlockId = new Map(
    document.blocks
      .filter((block) => block.type === "heading")
      .map((block) => [block.id, block.sectionId || block.id]),
  )
  const fromDocument = document.outline
    .map((item, index) => outlineItemFromDocument(item, index, sectionById, headingSectionIdByBlockId))
    .filter((item): item is NonNullable<typeof item> => Boolean(item))
  if (fromDocument.length) return fromDocument
  return Array.from(sections.values())
    .sort((left, right) => (left.sourceOrder ?? Number.MAX_SAFE_INTEGER) - (right.sourceOrder ?? Number.MAX_SAFE_INTEGER))
    .map((section, index) => ({
      id: section.id,
      title: section.title,
      sectionType: section.sectionType,
      level: normalizeLevel(section.level),
      sectionNumber: section.sectionNumber,
      sourceOrder: section.sourceOrder ?? index,
      paragraphCount: 0,
    }))
}

function outlineItemFromDocument(
  item: PaperDocument["outline"][number],
  index: number,
  sections: Map<string, MutableSection>,
  headingSectionIdByBlockId: Map<string, string>,
) {
  const blockId = optionalText(item.blockId)
  const itemId = optionalText(item.id)
  const sectionId = (blockId ? headingSectionIdByBlockId.get(blockId) : undefined) ?? itemId
  if (!sectionId) return null
  const section = sections.get(sectionId)
  if (!section && !blockId) return null
  const normalized = normalizeOutlineTitle(item.title, item.sectionNumber)
  const title = section?.title || normalized.title
  return {
    id: section?.id ?? sectionId,
    title,
    sectionType: section?.sectionType ?? sectionTypeFromTitle(title),
    level: normalizeLevel(section?.level ?? item.level),
    sectionNumber: section?.sectionNumber ?? normalized.sectionNumber,
    sourceOrder: section?.sourceOrder ?? index,
    paragraphCount: 0,
  }
}

function normalizeOutlineTitle(title: string, sectionNumber?: unknown) {
  const metadataNumber = optionalText(sectionNumber)
  const parsed = splitLeadingSectionNumber(title)
  return {
    title: parsed && (!metadataNumber || parsed.sectionNumber === metadataNumber) ? parsed.title : title,
    sectionNumber: metadataNumber ?? parsed?.sectionNumber,
  }
}

function minNumber(left: number | undefined, right: number | undefined) {
  if (right === undefined) return left
  if (left === undefined) return right
  return Math.min(left, right)
}

function maxNumber(left: number | undefined, right: number | undefined) {
  if (right === undefined) return left
  if (left === undefined) return right
  return Math.max(left, right)
}

function readInlineSpans(block: PaperBlock): PaperInlineSpan[] {
  const spans = block.metadata?.inlineSpans
  if (!Array.isArray(spans)) return []
  return spans.filter(isInlineSpan)
}

function isInlineSpan(value: unknown): value is PaperInlineSpan {
  if (!value || typeof value !== "object") return false
  const span = value as Partial<PaperInlineSpan>
  return typeof span.type === "string"
    && typeof span.text === "string"
    && typeof span.start === "number"
    && typeof span.end === "number"
}

function readReferences(auxiliary: Record<string, unknown> | undefined): PaperReference[] {
  const references = auxiliary?.references
  if (!Array.isArray(references)) return []
  return references.filter(isPaperReference)
}

function isPaperReference(value: unknown): value is PaperReference {
  if (!value || typeof value !== "object") return false
  const reference = value as Partial<PaperReference>
  return typeof reference.id === "string"
    && typeof reference.key === "string"
    && typeof reference.number === "number"
    && typeof reference.label === "string"
    && typeof reference.text === "string"
}
