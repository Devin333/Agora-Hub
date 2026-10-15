import { z } from "zod"

export const PERSONAL_MEMORY_CATEGORIES = [
  "research_interest",
  "reading_preference",
  "background",
] as const
export const PERSONAL_MEMORY_STATUSES = ["candidate", "active", "disabled", "deleted"] as const
export const PERSONAL_MEMORY_SOURCE_TYPES = [
  "user_input",
  "reading_note",
  "paper",
  "evidence",
  "conversation",
  "import",
  "other",
] as const

export type PersonalMemoryCategory = (typeof PERSONAL_MEMORY_CATEGORIES)[number]
export type PersonalMemoryStatus = (typeof PERSONAL_MEMORY_STATUSES)[number]
export type PersonalMemorySourceType = (typeof PERSONAL_MEMORY_SOURCE_TYPES)[number]

export type PersonalMemorySourceRef = {
  source_type: PersonalMemorySourceType
  source_ref: string
  source_revision: string | null
  locator: string | null
  label: string | null
}

export type PersonalMemoryRecord = {
  memory_id: string
  content: string
  category: PersonalMemoryCategory
  source_refs: PersonalMemorySourceRef[]
  status: PersonalMemoryStatus
  revision: number
  created_at: string
  updated_at: string
  origin: "model_candidate" | "user_confirmed"
  confirmed_at: string | null
  disabled_at: string | null
  deleted_at: string | null
}

export type PersonalMemoryRemoteProcessingPermissions = {
  model: boolean
  tool: boolean
  embedding: boolean
}

export type PersonalMemoryPolicy = {
  allow_remote_processing: boolean
  remote_processing_permissions?: PersonalMemoryRemoteProcessingPermissions
  revision: number
  created_at: string
  updated_at: string
}

export type PersonalMemoryList = {
  records: PersonalMemoryRecord[]
  total: number
  next_cursor: string | null
}

export type PersonalMemoryRecall = {
  records: Array<PersonalMemoryRecord & { score: number; reasons: string[] }>
  used_memory_refs: Array<{ memory_id: string; revision: number }>
  query: string
  memory_enabled: boolean
}

export type PersonalMemoryExport = {
  schema_version: string
  exported_at: string
  records: PersonalMemoryRecord[]
  policy: PersonalMemoryPolicy
  notices: string[]
}

const sourceRefSchema = z.object({
  source_type: z.enum(PERSONAL_MEMORY_SOURCE_TYPES),
  source_ref: z.string().min(1).max(1024),
  source_revision: z.string().min(1).max(256).optional(),
  locator: z.string().min(1).max(512).optional(),
  label: z.string().min(1).max(256).optional(),
}).strict()

const contentFields = {
  content: z.string().min(1).max(8000),
  category: z.enum(PERSONAL_MEMORY_CATEGORIES),
  source_refs: z.array(sourceRefSchema).min(1).max(32),
}

const mutableFields = {
  content: z.string().min(1).max(8000).optional(),
  category: z.enum(PERSONAL_MEMORY_CATEGORIES).optional(),
  source_refs: z.array(sourceRefSchema).min(1).max(32).optional(),
}

const createSchema = z.object({
  creation: z.enum(["confirmed", "candidate"]),
  request_id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/),
  ...contentFields,
}).strict()

const updateSchema = z.object({
  revision: z.number().int().positive(),
  ...mutableFields,
}).strict().refine(
  (value) => value.content !== undefined || value.category !== undefined || value.source_refs !== undefined,
  "at least one mutable field is required",
)

const confirmSchema = z.object({
  revision: z.number().int().positive(),
  ...mutableFields,
}).strict()

const revisionSchema = z.object({ revision: z.number().int().positive() }).strict()
const statusSchema = z.object({
  status: z.enum(["active", "disabled"]),
  revision: z.number().int().positive(),
}).strict()
const recallSchema = z.object({
  query: z.string().min(1).max(512),
  limit: z.number().int().min(1).max(20).optional(),
  use_memory: z.boolean().optional(),
}).strict()
const policySchema = z.object({
  allow_remote_processing: z.boolean(),
  revision: z.number().int().positive(),
}).strict()

export function sanitizePersonalMemoryCreate(value: Record<string, unknown>) {
  return sanitize(createSchema, value)
}
export function sanitizePersonalMemoryUpdate(value: Record<string, unknown>) {
  return sanitize(updateSchema, value)
}
export function sanitizePersonalMemoryConfirm(value: Record<string, unknown>) {
  return sanitize(confirmSchema, value)
}
export function sanitizePersonalMemoryIndexRebuild(value: Record<string, unknown>) {
  return sanitize(z.object({}).strict(), value)
}
export function sanitizePersonalMemoryRevision(value: Record<string, unknown>) {
  return sanitize(revisionSchema, value)
}
export function sanitizePersonalMemoryStatus(value: Record<string, unknown>) {
  return sanitize(statusSchema, value)
}
export function sanitizePersonalMemoryRecall(value: Record<string, unknown>) {
  return sanitize(recallSchema, value)
}
export function sanitizePersonalMemoryPolicy(value: Record<string, unknown>) {
  return sanitize(policySchema, value)
}

export function isPersonalMemoryId(value: string) {
  return /^pmem_[0-9a-f]{32}$/.test(value)
}

export function personalMemoryListPath(searchParams: URLSearchParams): string | null {
  const allowed = new Set(["limit", "cursor", "status"])
  if ([...searchParams.keys()].some((key) => !allowed.has(key))) return null
  const limit = searchParams.get("limit")
  if (limit !== null && (!/^\d{1,3}$/.test(limit) || Number(limit) < 1 || Number(limit) > 100)) return null
  const cursor = searchParams.get("cursor")
  if (cursor !== null && (!/^\d{1,5}$/.test(cursor) || Number(cursor) > 10000)) return null
  const statuses = searchParams.getAll("status")
  if (statuses.length > 4 || new Set(statuses).size !== statuses.length) return null
  if (statuses.some((status) => !(PERSONAL_MEMORY_STATUSES as readonly string[]).includes(status))) return null
  const query = new URLSearchParams()
  if (limit !== null) query.set("limit", limit)
  if (cursor !== null) query.set("cursor", cursor)
  statuses.forEach((status) => query.append("status", status))
  const suffix = query.toString()
  return "/api/v1/research/desktop/memory" + (suffix ? `?${suffix}` : "")
}

export function personalMemoryExportPath(searchParams: URLSearchParams): string | null {
  if ([...searchParams.keys()].some((key) => key !== "include_deleted")) return null
  const value = searchParams.get("include_deleted")
  if (value !== null && value !== "true" && value !== "false") return null
  return "/api/v1/research/desktop/memory/export" + (value === null ? "" : `?include_deleted=${value}`)
}

function sanitize<T extends z.ZodType<Record<string, unknown>>>(schema: T, value: Record<string, unknown>) {
  const parsed = schema.safeParse(value)
  return parsed.success ? parsed.data : null
}
