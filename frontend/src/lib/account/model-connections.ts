import { z } from "zod"
import { accountRequest, jsonBody } from "./client"

// Browser client for the account's model connections. A connection is one
// provider + model + key; each model purpose (answer, evidence summary, ...)
// is assigned one connection. Keys are write-only: views carry a hint at most.

const purposeIdSchema = z.enum(["answer", "evidence_summary", "section_navigation", "candidate_generation"])
export type ModelPurposeId = z.infer<typeof purposeIdSchema>
export const MODEL_PURPOSES = purposeIdSchema.options

const connectionStatusSchema = z.enum(["verified", "unverified", "key_missing", "key_rotated", "withdrawn"])
export type ModelConnectionStatus = z.infer<typeof connectionStatusSchema>

const effortSchema = z.enum(["off", "minimal", "low", "medium", "high", "xhigh", "max"])
export type ReasoningEffort = z.infer<typeof effortSchema>
export const REASONING_EFFORTS = effortSchema.options

const fitSchema = z.object({
  fits: z.boolean(),
  requiredWindow: z.number().int().positive().nullable(),
  reason: z.string().nullable(),
}).strip()
export type PurposeFit = z.infer<typeof fitSchema>

export const modelConnectionSchema = z.object({
  id: z.string().min(1),
  label: z.string().min(1),
  providerId: z.string().min(1),
  providerLabel: z.string().min(1),
  custom: z.boolean(),
  protocol: z.string().nullable(),
  baseUrl: z.string().nullable(),
  model: z.string().min(1),
  modelLabel: z.string().min(1),
  reasoningEffort: effortSchema.nullable(),
  reasoningParameter: z.string().nullable(),
  contextWindowTokens: z.number().int().positive().nullable(),
  maxOutputTokens: z.number().int().positive().nullable(),
  structuredOutput: z.string().nullable(),
  status: connectionStatusSchema,
  credentialConfigured: z.boolean(),
  keyHint: z.string().nullable(),
  revision: z.number().int().positive(),
  createdAt: z.string(),
  updatedAt: z.string(),
  verifiedAt: z.string().nullable(),
  purposes: z.array(purposeIdSchema),
  fits: z.record(purposeIdSchema, fitSchema),
}).strip()
export type ModelConnection = z.infer<typeof modelConnectionSchema>

const requirementSchema = z.object({
  inputTokens: z.number().int().positive(),
  outputTokens: z.number().int().positive(),
  structuredOutput: z.boolean(),
}).strip()
export type PurposeRequirement = z.infer<typeof requirementSchema>

const purposeSchema = z.object({
  id: purposeIdSchema,
  label: z.string().min(1),
  requirement: requirementSchema,
}).strip()
export type ModelPurpose = z.infer<typeof purposeSchema>

const catalogReasoningSchema = z.object({
  parameter: z.string().min(1),
  efforts: z.array(effortSchema),
  default: effortSchema.nullable(),
}).strip()

const catalogModelSchema = z.object({
  id: z.string().min(1),
  label: z.string().min(1),
  contextWindowTokens: z.number().int().positive(),
  maxOutputTokens: z.number().int().positive().nullable(),
  reasoning: catalogReasoningSchema,
  structuredOutput: z.boolean(),
}).strip()
export type CatalogModel = z.infer<typeof catalogModelSchema>

const catalogProviderSchema = z.object({
  id: z.string().min(1),
  label: z.string().min(1),
  kind: z.literal("catalog"),
  protocol: z.string().min(1),
  baseUrl: z.string().min(1),
  defaultModel: z.string().min(1),
  models: z.array(catalogModelSchema).min(1),
}).strip()
export type CatalogProvider = z.infer<typeof catalogProviderSchema>

const customProviderSchema = z.object({
  id: z.string().min(1),
  label: z.string().min(1),
  kind: z.literal("custom"),
  protocol: z.string().min(1),
  reasoningParameters: z.array(z.string().min(1)).min(1),
  reasoningEfforts: z.record(z.string(), z.array(effortSchema)),
  structuredOutputModes: z.array(z.string().min(1)).min(1),
}).strip()
export type CustomProvider = z.infer<typeof customProviderSchema>

const providerSchema = z.discriminatedUnion("kind", [catalogProviderSchema, customProviderSchema])
export type ModelProvider = z.infer<typeof providerSchema>

export const modelConnectionsViewSchema = z.object({
  revision: z.number().int().nonnegative(),
  connections: z.array(modelConnectionSchema),
  assignments: z.record(purposeIdSchema, z.string().nullable()),
  purposes: z.array(purposeSchema),
  reasoningOutputAllowance: z.record(z.string(), z.number().int().nonnegative()),
  catalog: z.object({ providers: z.array(providerSchema) }).strip(),
}).strip()
export type ModelConnectionsView = z.infer<typeof modelConnectionsViewSchema>

/** What the connection form submits; omitted fields are not sent. */
export type ModelConnectionInput = {
  providerId: string
  model: string
  label?: string
  reasoningEffort?: ReasoningEffort | null
  apiKey?: string
  baseUrl?: string
  contextWindowTokens?: number
  maxOutputTokens?: number
  reasoningParameter?: string
  structuredOutput?: string
}

const PATH = "/api/v1/account/model-connections"

function connectionPath(connectionId: string, action?: "verify") {
  return `${PATH}/${encodeURIComponent(connectionId)}${action ? `/${action}` : ""}`
}

function inputBody(input: ModelConnectionInput, expectedRevision: number): Record<string, unknown> {
  const body: Record<string, unknown> = { providerId: input.providerId, model: input.model, expectedRevision }
  for (const key of [
    "label", "reasoningEffort", "apiKey", "baseUrl", "contextWindowTokens",
    "maxOutputTokens", "reasoningParameter", "structuredOutput",
  ] as const) {
    const value = input[key]
    if (value !== undefined && value !== null && value !== "") body[key] = value
  }
  return body
}

export function fetchModelConnections(signal?: AbortSignal): Promise<ModelConnectionsView> {
  return accountRequest(modelConnectionsViewSchema, PATH, { signal })
}

export function createModelConnection(input: ModelConnectionInput, expectedRevision: number): Promise<ModelConnectionsView> {
  return accountRequest(modelConnectionsViewSchema, PATH, jsonBody("POST", inputBody(input, expectedRevision)))
}

export function updateModelConnection(
  connectionId: string,
  input: ModelConnectionInput,
  expectedRevision: number,
): Promise<ModelConnectionsView> {
  return accountRequest(modelConnectionsViewSchema, connectionPath(connectionId), jsonBody("PATCH", inputBody(input, expectedRevision)))
}

export function deleteModelConnection(connectionId: string, expectedRevision: number): Promise<ModelConnectionsView> {
  const query = new URLSearchParams({ expectedRevision: String(expectedRevision) })
  return accountRequest(modelConnectionsViewSchema, `${connectionPath(connectionId)}?${query}`, { method: "DELETE" })
}

export function verifyModelConnection(connectionId: string): Promise<ModelConnectionsView> {
  return accountRequest(modelConnectionsViewSchema, connectionPath(connectionId, "verify"), jsonBody("POST", {}))
}

export function assignModelConnections(
  assignments: Partial<Record<ModelPurposeId, string | null>>,
  expectedRevision: number,
): Promise<ModelConnectionsView> {
  return accountRequest(modelConnectionsViewSchema, `${PATH}/assignments`, jsonBody("PUT", { assignments, expectedRevision }))
}

export async function discoverModels(input: {
  providerId: string
  baseUrl?: string
  apiKey?: string
  connectionId?: string
}): Promise<string[]> {
  const body: Record<string, unknown> = { providerId: input.providerId }
  if (input.baseUrl) body.baseUrl = input.baseUrl
  if (input.apiKey) body.apiKey = input.apiKey
  if (input.connectionId) body.connectionId = input.connectionId
  const result = await accountRequest(z.object({ models: z.array(z.string()) }).strip(), `${PATH}/discover`, jsonBody("POST", body))
  return result.models
}
