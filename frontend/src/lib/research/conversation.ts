import { z } from "zod"

const text = (max: number) => z.string().max(max).refine(value => !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value))
const nonblank = (max: number) => text(max).refine(value => Boolean(value.trim()))
const id = z.string().regex(/^[A-Za-z0-9:_-]{1,128}$/)
const time = z.number().int().positive().max(Number.MAX_SAFE_INTEGER)
const source = z.string().max(2000).url().refine(value => { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password && !/[\\\r\n]/.test(value) })
const localHref = z.string().max(10000).refine(value => {
  if (!value.startsWith("/") || value.startsWith("//") || /[\\\r\n]/.test(value)) return false
  const url = new URL(value, "https://agora.invalid")
  return url.origin === "https://agora.invalid" && /^\/(?:design-demo\/)?(?:papers|projects)(?:\/[A-Za-z0-9_-]+(?:\/read)?)?$/.test(url.pathname)
})

export const researchSourceSchema = z.enum(["papers", "projects"])
export type ResearchSource = z.infer<typeof researchSourceSchema>
export const conversationConstraintsSchema = z.object({
  recentYear: z.boolean().optional(), hasCode: z.boolean().optional(), paperType: z.literal("survey").optional(),
  language: z.enum(["python", "typescript", "javascript", "rust", "go"]).optional(),
  license: z.enum(["MIT", "Apache-2.0", "BSD-3-Clause"]).optional(),
  recentlyActive: z.boolean().optional(), localRunnable: z.boolean().optional(),
}).strict()
export const researchIntentSchema = z.object({
  summary: nonblank(500), query: nonblank(2000),
  sources: z.array(researchSourceSchema).min(1).max(2).refine(values => new Set(values).size === values.length),
  constraints: conversationConstraintsSchema,
  clarification: z.object({ question: nonblank(300), options: z.array(nonblank(120)).min(2).max(3) }).strict().nullable(),
  confirmationRequired: z.boolean().optional(),
  changeNotice: text(300).optional(),
}).strict()
export type ResearchIntent = z.infer<typeof researchIntentSchema>
export const researchResultSchema = z.object({
  id: nonblank(200), kind: researchSourceSchema, title: nonblank(500),
  description: text(2000), source: text(200), url: source, href: localHref.optional(),
  authors: text(1000).optional(), publishedAt: text(100).optional(),
  language: text(100).optional(), stars: z.number().int().nonnegative().optional(),
}).strict()
export type ResearchResult = z.infer<typeof researchResultSchema>
export const researchSearchResponseSchema = z.object({
  source: researchSourceSchema, results: z.array(researchResultSchema).max(10),
  total: z.number().int().nonnegative(), moreHref: localHref.optional(),
}).strict().refine(value => value.results.every(result => result.kind === value.source))
export type ResearchSearchResponse = z.infer<typeof researchSearchResponseSchema>

export const conversationPhaseSchema = z.enum(["understanding", "clarifying", "confirming", "searching", "results", "stopped", "error"])
export type ConversationPhase = z.infer<typeof conversationPhaseSchema>
export const researchTurnSchema = z.object({
  id, question: nonblank(2000), answers: z.array(nonblank(2000)).max(2),
  materialIds: z.array(id).max(50).refine(values => new Set(values).size === values.length).optional(),
  phase: conversationPhaseSchema, intent: researchIntentSchema.optional(),
  requestedSources: z.array(researchSourceSchema).min(1).max(2).optional(),
  requestedConstraints: conversationConstraintsSchema.optional(),
  searches: z.array(researchSearchResponseSchema).max(2).refine(values => new Set(values.map(value => value.source)).size === values.length),
  failures: z.array(z.object({ source: researchSourceSchema, message: nonblank(500) }).strict()).max(2),
  error: text(500).optional(),
  events: z.array(z.object({ phase: conversationPhaseSchema, at: time }).strict()).min(1).max(50),
  createdAt: time,
}).strict()
export type ResearchTurn = z.infer<typeof researchTurnSchema>
export const researchConversationSchema = z.object({
  version: z.literal(1), mode: z.enum(["auto", "plan"]),
  materialIds: z.array(id).max(50).refine(values => new Set(values).size === values.length),
  turns: z.array(researchTurnSchema).min(1).max(20),
  draft: text(2000),
}).strict().refine(value => new Set(value.turns.map(turn => turn.id)).size === value.turns.length)
export type ResearchConversation = z.infer<typeof researchConversationSchema>
export const validResearchConversation = (value: unknown): value is ResearchConversation => researchConversationSchema.safeParse(value).success

export function conversationHref(sessionId: string) {
  return `/design-demo?researchSession=${encodeURIComponent(sessionId)}`
}

export function transitionResearchTurn(turn: ResearchTurn, phase: ConversationPhase, patch: Partial<ResearchTurn> = {}): ResearchTurn {
  return { ...turn, ...patch, phase, events: [...turn.events, { phase, at: Date.now() }] }
}
