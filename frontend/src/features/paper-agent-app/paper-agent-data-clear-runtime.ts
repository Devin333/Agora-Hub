"use client"

import { z } from "zod"

export const DATA_CLEAR_CONFIRMATION_TEXT = "删除本机全部 Agora 数据" as const

const dataClearBaseFields = {
  ok: z.boolean(),
  state: z.string().min(1).max(100),
  code: z.string().min(1).max(160),
  message: z.string().max(2000),
  cancelled: z.boolean().optional(),
}

const dataClearScopeSchema = z.object({
  profilePath: z.string().min(1).max(2048),
  fileCount: z.number().int().nonnegative(),
  totalBytes: z.number().int().nonnegative(),
  includes: z.array(z.string().min(1).max(240)).min(1).max(50),
  preserves: z.array(z.string().min(1).max(240)).min(1).max(50),
  credentialPolicy: z.string().min(1).max(1000),
}).strict()

const isoTimestampSchema = z.string().max(100).datetime({ offset: true })

const dataClearPreviewResultSchema = z.object({
  ...dataClearBaseFields,
  previewId: z.string().min(1).max(512).optional(),
  expiresAt: isoTimestampSchema.optional(),
  scope: dataClearScopeSchema.optional(),
  stopped: z.boolean().optional(),
}).strict().superRefine((result, context) => {
  if (!result.ok) return
  if (result.state !== "CONFIRMATION_REQUIRED") context.addIssue({ code: z.ZodIssueCode.custom, path: ["state"], message: "successful preview requires CONFIRMATION_REQUIRED" })
  if (!result.previewId) context.addIssue({ code: z.ZodIssueCode.custom, path: ["previewId"], message: "successful preview requires previewId" })
  if (!result.expiresAt) context.addIssue({ code: z.ZodIssueCode.custom, path: ["expiresAt"], message: "successful preview requires expiresAt" })
  if (!result.scope) context.addIssue({ code: z.ZodIssueCode.custom, path: ["scope"], message: "successful preview requires scope" })
  if (result.stopped !== true) context.addIssue({ code: z.ZodIssueCode.custom, path: ["stopped"], message: "successful preview requires a safely stopped runtime" })
})

const dataClearApplyResultSchema = z.object({
  ...dataClearBaseFields,
  operationId: z.string().min(1).max(512).optional(),
  exitRequired: z.boolean().optional(),
}).strict().superRefine((result, context) => {
  if (result.state === "CLEARED") context.addIssue({ code: z.ZodIssueCode.custom, path: ["state"], message: "apply cannot report final deletion" })
  if (!result.ok) return
  if (result.state !== "PREPARED") context.addIssue({ code: z.ZodIssueCode.custom, path: ["state"], message: "accepted apply must be PREPARED" })
  if (!result.operationId) context.addIssue({ code: z.ZodIssueCode.custom, path: ["operationId"], message: "PREPARED requires operationId" })
  if (result.exitRequired !== true) context.addIssue({ code: z.ZodIssueCode.custom, path: ["exitRequired"], message: "PREPARED requires host exit" })
})

const dataClearStatusResultSchema = z.object({
  ...dataClearBaseFields,
  operationId: z.string().min(1).max(512).optional(),
  completedAt: isoTimestampSchema.optional(),
}).strict().superRefine((result, context) => {
  if (result.state !== "CLEARED") return
  if (!result.ok) context.addIssue({ code: z.ZodIssueCode.custom, path: ["ok"], message: "CLEARED must be authoritative" })
  if (!result.operationId) context.addIssue({ code: z.ZodIssueCode.custom, path: ["operationId"], message: "CLEARED requires operationId" })
  if (!result.completedAt) context.addIssue({ code: z.ZodIssueCode.custom, path: ["completedAt"], message: "CLEARED requires completedAt" })
})

const dataClearApplyRequestSchema = z.object({
  previewId: z.string().min(1).max(512),
  confirmationText: z.literal(DATA_CLEAR_CONFIRMATION_TEXT),
}).strict()

export type DesktopDataClearPreviewResult = z.infer<typeof dataClearPreviewResultSchema>
export type DesktopDataClearApplyResult = z.infer<typeof dataClearApplyResultSchema>
export type DesktopDataClearStatusResult = z.infer<typeof dataClearStatusResultSchema>

interface DesktopDataClearBridge {
  preview(): Promise<unknown>
  apply(input: unknown): Promise<unknown>
  status(): Promise<unknown>
}

type DesktopWindow = Window & {
  agoraDesktop?: {
    version?: unknown
    runtime?: {
      clearData?: DesktopDataClearBridge
    }
  }
}

export function hasDesktopDataClearBridge(): boolean {
  return getDesktopDataClearBridge() !== null
}

export async function previewDesktopDataClear(): Promise<DesktopDataClearPreviewResult> {
  return dataClearPreviewResultSchema.parse(await requireDesktopDataClearBridge().preview())
}

export async function applyDesktopDataClear(previewId: string, confirmationText: string): Promise<DesktopDataClearApplyResult> {
  const request = dataClearApplyRequestSchema.parse({ previewId, confirmationText })
  return dataClearApplyResultSchema.parse(await requireDesktopDataClearBridge().apply(request))
}

export async function readDesktopDataClearStatus(): Promise<DesktopDataClearStatusResult> {
  return dataClearStatusResultSchema.parse(await requireDesktopDataClearBridge().status())
}

function getDesktopDataClearBridge(): DesktopDataClearBridge | null {
  if (typeof window === "undefined") return null
  const desktop = (window as DesktopWindow).agoraDesktop
  if (desktop?.version !== 1) return null
  const bridge = desktop.runtime?.clearData
  return bridge
    && typeof bridge.preview === "function"
    && typeof bridge.apply === "function"
    && typeof bridge.status === "function"
    ? bridge
    : null
}

function requireDesktopDataClearBridge(): DesktopDataClearBridge {
  const bridge = getDesktopDataClearBridge()
  if (!bridge) throw new Error("desktop_data_clear_unavailable")
  return bridge
}
