import type {
  ResearchAnalyzeSemantic,
  ResearchAskSemantic,
  ResearchPrepareSemantic,
} from "@/lib/research/workspace-items"

const SHA256_PATTERN = /^sha256:[0-9a-f]{64}$/
const GENERATION_PATTERN = /^gen_[0-9a-f]{32}$/
const SAFE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/
const OWNER_SCOPE_FIELDS = new Set([
  "userId",
  "user_id",
  "tenantId",
  "tenant_id",
  "memoryNamespace",
  "memory_namespace",
])

const PREPARE_FIELDS = new Set([
  "requestId",
  "generation",
  "requestFingerprint",
  "paperId",
  "sourceUrl",
  "sourceType",
])

const ANALYZE_FIELDS = new Set([
  "requestId",
  "generation",
  "requestFingerprint",
  "paperId",
  "sourceRevision",
  "sourceFingerprint",
  "sourceUrl",
  "pdfUrl",
  "objective",
  "constraints",
  "personalMemory",
])

const ASK_FIELDS = new Set([
  "requestId",
  "generation",
  "requestFingerprint",
  "runId",
  "paperId",
  "sourceRevision",
  "sourceFingerprint",
  "question",
  "selection",
  "locale",
  "personalMemory",
])

export type PreparedRequestIdentity = {
  requestId: string
  generation: string
  fingerprint: string
}

export function normalizeAnalyzeTransportUrl(value?: string | null): string | null {
  if (!value || value.startsWith("/")) return null
  try {
    const url = new URL(value)
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null
    return value
  } catch {
    return null
  }
}

export function buildPrepareRequest(
  identity: PreparedRequestIdentity,
  semantic: ResearchPrepareSemantic,
): Record<string, unknown> {
  return {
    requestId: identity.requestId,
    generation: identity.generation,
    requestFingerprint: identity.fingerprint,
    paperId: semantic.paperId,
    sourceUrl: semantic.sourceUrl,
    sourceType: semantic.sourceType,
  }
}

export function sanitizePrepareRequest(
  body: Record<string, unknown>,
): Record<string, unknown> | null {
  if (hasOwnerScope(body) || hasUnknownFields(body, PREPARE_FIELDS)) return null
  if (!validRequestIdentity(body)) return null
  if (!validText(body.paperId, 1, 256) || !SAFE_ID_PATTERN.test(body.paperId)) return null
  if (typeof body.sourceUrl !== "string" || normalizeAnalyzeTransportUrl(body.sourceUrl) !== body.sourceUrl) {
    return null
  }
  if (body.sourceType !== null && body.sourceType !== undefined && !validText(body.sourceType, 1, 64)) {
    return null
  }
  return body
}

export function buildAnalyzeRequest(
  identity: PreparedRequestIdentity,
  semantic: ResearchAnalyzeSemantic,
): Record<string, unknown> {
  return {
    requestId: identity.requestId,
    generation: identity.generation,
    requestFingerprint: identity.fingerprint,
    paperId: semantic.paperId,
    sourceRevision: semantic.sourceRevision,
    sourceFingerprint: semantic.sourceFingerprint,
    sourceUrl: normalizeAnalyzeTransportUrl(semantic.sourceUrl),
    pdfUrl: normalizeAnalyzeTransportUrl(semantic.pdfUrl),
    objective: semantic.objective,
    constraints: semantic.constraints,
    ...(semantic.personalMemory ? { personalMemory: semantic.personalMemory } : {}),
  }
}

export function buildAskRequest(
  identity: PreparedRequestIdentity,
  semantic: ResearchAskSemantic,
): Record<string, unknown> {
  return {
    requestId: identity.requestId,
    generation: identity.generation,
    requestFingerprint: identity.fingerprint,
    runId: semantic.runId,
    paperId: semantic.paperId,
    sourceRevision: semantic.sourceRevision,
    sourceFingerprint: semantic.sourceFingerprint,
    question: semantic.question,
    selection: semantic.selection,
    locale: semantic.locale,
    ...(semantic.personalMemory ? { personalMemory: semantic.personalMemory } : {}),
  }
}

export function sanitizeAnalyzeRequest(
  body: Record<string, unknown>,
): Record<string, unknown> | null {
  if (hasOwnerScope(body) || hasUnknownFields(body, ANALYZE_FIELDS)) return null
  if (!validRequestIdentity(body)) return null
  if (!validText(body.paperId, 1, 256) || !SAFE_ID_PATTERN.test(body.paperId)) return null
  if (!validText(body.sourceRevision, 1, 512)) return null
  if (typeof body.sourceFingerprint !== "string" || !SHA256_PATTERN.test(body.sourceFingerprint)) return null
  if (!validAnalyzeTransportUrl(body.sourceUrl) || !validAnalyzeTransportUrl(body.pdfUrl)) return null
  if (!validNullableText(body.objective, 2000)) return null
  if (body.personalMemory !== undefined && !validPersonalMemorySelection(body.personalMemory)) return null
  if (
    body.constraints !== undefined &&
    (!Array.isArray(body.constraints) ||
      body.constraints.length > 20 ||
      body.constraints.some(value => !validText(value, 1, 500) || !value.trim()))
  ) {
    return null
  }
  return body
}

export function sanitizeAskRequest(
  body: Record<string, unknown>,
): Record<string, unknown> | null {
  if (hasOwnerScope(body) || hasUnknownFields(body, ASK_FIELDS)) return null
  if (!validRequestIdentity(body)) return null
  if (!validText(body.runId, 1, 256) || !SAFE_ID_PATTERN.test(body.runId)) return null
  if (!validText(body.paperId, 1, 256) || !SAFE_ID_PATTERN.test(body.paperId)) return null
  if (!validText(body.sourceRevision, 1, 512)) return null
  if (typeof body.sourceFingerprint !== "string" || !SHA256_PATTERN.test(body.sourceFingerprint)) return null
  if (!validText(body.question, 1, 2000) || !body.question.trim()) return null
  if (!validSelection(body.selection)) return null
  if (body.locale !== undefined && body.locale !== null && !validText(body.locale, 1, 32)) return null
  if (body.personalMemory !== undefined && !validPersonalMemorySelection(body.personalMemory)) return null
  return body
}

export function sanitizeSavedReport(
  body: Record<string, unknown>,
): Record<string, unknown> | null {
  const allowed = new Set(["reportId", "runId", "sourceRevision", "title"])
  if (hasOwnerScope(body) || hasUnknownFields(body, allowed)) return null
  if (!validText(body.reportId, 4, 128) || !SAFE_ID_PATTERN.test(body.reportId)) return null
  if (!validText(body.runId, 1, 256) || !SAFE_ID_PATTERN.test(body.runId)) return null
  if (!validText(body.sourceRevision, 1, 512)) return null
  if (!validText(body.title, 1, 500) || !body.title.trim()) return null
  return body
}

export function sanitizeDraftExport(
  body: Record<string, unknown>,
): Record<string, unknown> | null {
  const allowed = new Set(["title", "question", "notes", "materials", "format"])
  if (hasOwnerScope(body) || hasUnknownFields(body, allowed)) return null
  if (!validText(body.title, 1, 500) || !body.title.trim()) return null
  if (!validText(body.question, 1, 2000) || !body.question.trim()) return null
  if (!validText(body.notes, 0, 30000)) return null
  if (body.format !== "markdown" && body.format !== "json") return null
  if (!Array.isArray(body.materials) || body.materials.length > 100) return null

  for (const value of body.materials) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null
    const material = value as Record<string, unknown>
    if (hasUnknownFields(material, new Set(["id", "title", "url", "notes"]))) return null
    if (!validText(material.id, 1, 128)) return null
    if (!validText(material.title, 1, 500)) return null
    if (!validText(material.url, 0, 2000)) return null
    if (!validText(material.notes, 0, 10000)) return null
  }
  return body
}

function validRequestIdentity(body: Record<string, unknown>): boolean {
  return (
    validText(body.requestId, 8, 128) &&
    typeof body.generation === "string" &&
    GENERATION_PATTERN.test(body.generation) &&
    typeof body.requestFingerprint === "string" &&
    SHA256_PATTERN.test(body.requestFingerprint)
  )
}

function validPersonalMemorySelection(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false
  const selection = value as Record<string, unknown>
  if (hasOwnerScope(selection) || hasUnknownFields(selection, new Set(["refs", "policyRevision"]))) return false
  if (typeof selection.policyRevision !== "number" || !Number.isSafeInteger(selection.policyRevision) || selection.policyRevision < 1) return false
  if (!Array.isArray(selection.refs) || selection.refs.length < 1 || selection.refs.length > 20) return false
  const ids = new Set<string>()
  for (const raw of selection.refs) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return false
    const ref = raw as Record<string, unknown>
    if (hasOwnerScope(ref) || hasUnknownFields(ref, new Set(["memoryId", "revision"]))) return false
    if (typeof ref.memoryId !== "string" || !/^pmem_[0-9a-f]{32}$/.test(ref.memoryId) || ids.has(ref.memoryId)) return false
    if (typeof ref.revision !== "number" || !Number.isSafeInteger(ref.revision) || ref.revision < 1) return false
    ids.add(ref.memoryId)
  }
  return true
}

function validSelection(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false
  const selection = value as Record<string, unknown>
  const allowed = new Set(["sectionId", "sectionIndex", "page", "pdfPage", "selectedText"])
  if (Object.keys(selection).some(key => !allowed.has(key))) return false
  if (
    selection.sectionId !== undefined &&
    !validText(selection.sectionId, 1, 200)
  ) {
    return false
  }
  if (
    selection.selectedText !== undefined &&
    !validText(selection.selectedText, 1, 10000)
  ) {
    return false
  }
  for (const field of ["sectionIndex", "page", "pdfPage"] as const) {
    const number = selection[field]
    if (
      number !== undefined &&
      (typeof number !== "number" ||
        !Number.isSafeInteger(number) ||
        number < 0 ||
        number > 10000)
    ) {
      return false
    }
  }
  return new TextEncoder().encode(JSON.stringify(selection)).byteLength <= 32000
}

function hasOwnerScope(body: Record<string, unknown>): boolean {
  return Object.keys(body).some(key => OWNER_SCOPE_FIELDS.has(key))
}

function hasUnknownFields(body: Record<string, unknown>, allowed: Set<string>): boolean {
  return Object.keys(body).some(key => !allowed.has(key))
}

function validNullableText(value: unknown, maximum: number): boolean {
  return value === undefined || value === null || validText(value, 0, maximum)
}

function validText(value: unknown, minimum: number, maximum: number): value is string {
  return typeof value === "string" && value.length >= minimum && value.length <= maximum
}

function validAnalyzeTransportUrl(value: unknown): value is string | null {
  if (value === null) return true
  return typeof value === "string" && normalizeAnalyzeTransportUrl(value) === value
}
