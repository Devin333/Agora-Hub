import type { PaperReaderCitation } from "@/lib/papers/types"

export type ReaderLocation = {
  sectionId?: string
  page?: number
}

export type ReaderLocationResolution =
  | { allowed: true; location: ReaderLocation }
  | { allowed: false; reason: "source_identity_unavailable" | "source_revision_mismatch" | "locator_unavailable" }

export function resolveRequestedReaderLocation(
  search: string,
  actualSourceRevision: string | undefined,
  sectionIds: ReadonlySet<string>,
  hasPdf: boolean,
): ReaderLocationResolution {
  const params = new URLSearchParams(search)
  const sectionId = params.get("resumeSection")?.trim() || undefined
  const rawPage = params.get("resumePage")
  const parsedPage = rawPage === null ? undefined : Number(rawPage)
  const page = Number.isSafeInteger(parsedPage) && Number(parsedPage) > 0
    ? Number(parsedPage)
    : undefined

  if (!sectionId && page === undefined) {
    return { allowed: true, location: {} }
  }

  const expectedRevision = params.get("sourceRevision")?.trim() || undefined
  if (!actualSourceRevision || !expectedRevision) {
    return { allowed: false, reason: "source_identity_unavailable" }
  }
  if (expectedRevision !== actualSourceRevision) {
    return { allowed: false, reason: "source_revision_mismatch" }
  }
  if ((sectionId && !sectionIds.has(sectionId)) || (page !== undefined && !hasPdf)) {
    return { allowed: false, reason: "locator_unavailable" }
  }
  return {
    allowed: true,
    location: {
      ...(sectionId ? { sectionId } : {}),
      ...(page !== undefined ? { page } : {}),
    },
  }
}

export function resolveCitationLocation(
  citation: PaperReaderCitation,
  actualSourceRevision: string | undefined,
  sectionIds: ReadonlySet<string>,
  hasPdf: boolean,
): ReaderLocationResolution {
  if (!actualSourceRevision || !citation.sourceRevision) {
    return { allowed: false, reason: "source_identity_unavailable" }
  }
  if (citation.sourceRevision !== actualSourceRevision) {
    return { allowed: false, reason: "source_revision_mismatch" }
  }
  if (citation.valid !== true) {
    return { allowed: false, reason: "locator_unavailable" }
  }

  const sectionId = citation.sectionId && sectionIds.has(citation.sectionId)
    ? citation.sectionId
    : undefined
  const page = Number.isSafeInteger(citation.page) && Number(citation.page) > 0 && hasPdf
    ? Number(citation.page)
    : undefined
  if (!sectionId && page === undefined) {
    return { allowed: false, reason: "locator_unavailable" }
  }
  return {
    allowed: true,
    location: {
      ...(sectionId ? { sectionId } : {}),
      ...(page !== undefined ? { page } : {}),
    },
  }
}
