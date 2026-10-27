import { z } from "zod"

export const personalMemoryCategorySchema = z.enum(["research_interest", "reading_preference", "background"])
export const personalMemoryStatusSchema = z.enum(["candidate", "active", "disabled", "deleted"])
export const personalMemorySourceRefSchema = z.object({
  source_type: z.enum(["user_input", "reading_note", "paper", "evidence", "conversation", "import", "other"]),
  source_ref: z.string().min(1).max(1024),
  source_revision: z.string().min(1).max(256).nullable().optional(),
  locator: z.string().min(1).max(512).nullable().optional(),
  label: z.string().min(1).max(256).nullable().optional(),
}).strict()
export const personalMemoryRecordSchema = z.object({
  memory_id: z.string().regex(/^pmem_[0-9a-f]{32}$/),
  content: z.string().min(1).max(8000),
  category: personalMemoryCategorySchema,
  source_refs: z.array(personalMemorySourceRefSchema).min(1).max(32),
  status: personalMemoryStatusSchema,
  revision: z.number().int().positive(),
  created_at: z.string().min(1).max(100),
  updated_at: z.string().min(1).max(100),
  origin: z.enum(["model_candidate", "user_confirmed"]).optional(),
  confirmed_at: z.string().nullable().optional(),
  disabled_at: z.string().nullable().optional(),
  deleted_at: z.string().nullable().optional(),
}).strip()
export const personalMemoryRemoteProcessingPermissionsSchema = z.object({
  model: z.boolean(),
  tool: z.boolean(),
  embedding: z.boolean(),
}).strict()
export const personalMemoryPolicySchema = z.object({
  allow_remote_processing: z.boolean(),
  remote_processing_permissions: personalMemoryRemoteProcessingPermissionsSchema.optional(),
  revision: z.number().int().positive(),
  created_at: z.string().min(1).max(100).optional(),
  updated_at: z.string().min(1).max(100).optional(),
}).strip()
export const personalMemoryInfoSchema = z.object({
  storage: z.string().optional(),
  status: z.string().optional(),
  remote_processing_default: z.boolean().optional(),
  remote_processing_channel_defaults: personalMemoryRemoteProcessingPermissionsSchema.optional(),
  remote_processing_channels: z.array(z.enum(["model", "tool", "embedding"])).max(3).optional(),
  remote_processing_auto_enabled: z.boolean().optional(),
  remote_processing_policy_endpoint: z.string().max(4096).optional(),
}).strip()

const memoryListSchema = z.object({ records: z.array(personalMemoryRecordSchema), total: z.number().int().nonnegative().default(0), next_cursor: z.string().nullable().default(null) }).strip()
const memoryRecordEnvelopeSchema = z.object({ record: personalMemoryRecordSchema }).strict()
const policyEnvelopeSchema = z.object({ policy: personalMemoryPolicySchema }).strict()
const infoEnvelopeSchema = z.object({ info: personalMemoryInfoSchema }).strict()
const memoryExportEnvelopeSchema = z.object({ export: z.object({
  schema_version: z.string(),
  exported_at: z.string(),
  records: z.array(personalMemoryRecordSchema),
  policy: personalMemoryPolicySchema,
  notices: z.array(z.string()),
}).strict() }).strict()

export type PersonalMemoryRecord = z.infer<typeof personalMemoryRecordSchema>
export type PersonalMemoryCategory = z.infer<typeof personalMemoryCategorySchema>
export type PersonalMemorySourceRef = z.infer<typeof personalMemorySourceRefSchema>
export type PersonalMemoryPolicy = z.infer<typeof personalMemoryPolicySchema>
export type PersonalMemoryInfo = z.infer<typeof personalMemoryInfoSchema>
export type PersonalMemoryCreation = "confirmed" | "candidate"

export class PersonalMemoryClientError extends Error {
  constructor(message: string, readonly status?: number, readonly code?: string) {
    super(message)
    this.name = "PersonalMemoryClientError"
  }
}

type ErrorEnvelope = { error?: { code?: string; message?: string }; success?: boolean; data?: unknown }
const basePath = "/api/research/personal-memory"

export async function listPersonalMemoryPage(options: { cursor?: string; limit?: number; statuses?: Array<z.infer<typeof personalMemoryStatusSchema>>; signal?: AbortSignal } = {}) {
  const query = new URLSearchParams({ limit: String(Math.max(1, Math.min(100, options.limit ?? 50))) })
  if (options.cursor) query.set("cursor", options.cursor)
  for (const status of options.statuses ?? []) query.append("status", status)
  const page = memoryListSchema.parse(await requestJson(`${basePath}?${query.toString()}`, { signal: options.signal }))
  return { records: page.records, total: page.total, nextCursor: page.next_cursor }
}
export async function getPersonalMemoryPolicy(signal?: AbortSignal) {
  return policyEnvelopeSchema.parse(await requestJson(`${basePath}/policy`, { signal })).policy
}
export async function updatePersonalMemoryPolicy(revision: number, allowRemoteProcessing: boolean, signal?: AbortSignal) {
  return policyEnvelopeSchema.parse(await requestJson(`${basePath}/policy`, json("PUT", { revision, allow_remote_processing: allowRemoteProcessing }, signal))).policy
}
export async function getPersonalMemoryInfo(signal?: AbortSignal) {
  return infoEnvelopeSchema.parse(await requestJson(`${basePath}/info`, { signal })).info
}
export async function createPersonalMemory(body: {
  content: string
  category: PersonalMemoryCategory
  source_refs: PersonalMemorySourceRef[]
  creation: PersonalMemoryCreation
  request_id: string
}, signal?: AbortSignal) {
  return memoryRecordEnvelopeSchema.parse(await requestJson(basePath, json("POST", body, signal))).record
}
export async function updatePersonalMemory(memoryId: string, revision: number, patch: Partial<Pick<PersonalMemoryRecord, "content" | "category" | "source_refs">>, signal?: AbortSignal) {
  return memoryRecordEnvelopeSchema.parse(await requestJson(`${basePath}/${encodeURIComponent(memoryId)}`, json("PATCH", { revision, ...patch }, signal))).record
}
export async function confirmPersonalMemory(memoryId: string, revision: number, signal?: AbortSignal) {
  return memoryRecordEnvelopeSchema.parse(await requestJson(`${basePath}/${encodeURIComponent(memoryId)}/confirm`, json("POST", { revision }, signal))).record
}
export async function setPersonalMemoryStatus(memoryId: string, revision: number, status: "active" | "disabled", signal?: AbortSignal) {
  return memoryRecordEnvelopeSchema.parse(await requestJson(`${basePath}/${encodeURIComponent(memoryId)}/status`, json("POST", { status, revision }, signal))).record
}
export async function rejectPersonalMemoryCandidate(memoryId: string, revision: number, signal?: AbortSignal) {
  return memoryRecordEnvelopeSchema.parse(await requestJson(`${basePath}/${encodeURIComponent(memoryId)}`, { ...json("DELETE", { revision }, signal) })).record
}
export async function deletePersonalMemory(memoryId: string, revision: number, signal?: AbortSignal) {
  return memoryRecordEnvelopeSchema.parse(await requestJson(`${basePath}/${encodeURIComponent(memoryId)}`, { ...json("DELETE", { revision }, signal) })).record
}
export async function rebuildPersonalMemoryIndex(signal?: AbortSignal): Promise<{ indexedRecords: number }> {
  const value = z.object({ indexed_records: z.number().int().nonnegative() }).strict().parse(
    await requestJson(`${basePath}/rebuild-index`, json("POST", {}, signal)),
  )
  return { indexedRecords: value.indexed_records }
}

export async function exportPersonalMemory(includeDeleted = false, signal?: AbortSignal): Promise<{ blob: Blob; filename: string }> {
  const payload = memoryExportEnvelopeSchema.parse(await requestJson(`${basePath}/export?include_deleted=${includeDeleted ? "true" : "false"}`, { signal })).export
  return {
    blob: new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }),
    filename: `agora-personal-memory-${new Date().toISOString().slice(0, 10)}.json`,
  }
}

async function requestJson(path: string, init: RequestInit): Promise<unknown> {
  const response = await fetch(path, { ...init, credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json", ...init.headers } })
  if (!response.ok) throw await responseError(response, "个人记忆服务暂时不可用。")
  const payload = await response.json() as ErrorEnvelope
  if (payload.success === false) throw new PersonalMemoryClientError(payload.error?.message ?? "个人记忆服务暂时不可用。", response.status, payload.error?.code)
  return payload.success === true && "data" in payload ? payload.data : payload
}
async function responseError(response: Response, fallback: string) {
  const payload = await response.json().catch(() => ({})) as ErrorEnvelope
  return new PersonalMemoryClientError(payload.error?.message ?? fallback, response.status, payload.error?.code)
}
function json(method: "POST" | "PATCH" | "PUT" | "DELETE", body: unknown, signal?: AbortSignal): RequestInit {
  return { method, signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
}

