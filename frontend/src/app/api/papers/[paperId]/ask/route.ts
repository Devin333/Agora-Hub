import { NextRequest, NextResponse } from "next/server"
import { safeApiPost } from "@/lib/api/server"
import { requirePublicPaper } from "@/lib/papers/public-route-guard"
import { z } from "zod"

export const dynamic = "force-dynamic"

const answerSchema = z.object({ answer: z.string(), evidenceRefs: z.array(z.string()).default([]), confidence: z.number().min(0).max(1), metadata: z.record(z.string(), z.unknown()).optional() })

export async function POST(request: NextRequest, { params }: { params: { paperId: string } }) {
  const guard = await requirePublicPaper(params.paperId)
  if (!guard.ok) {
    return guard.response
  }

  const body = await request.json().catch(() => ({}))
  const locale = body?.locale === "zh" ? "zh" : "en"
  const question = typeof body?.question === "string" ? body.question.trim() : ""
  if (!question || question.length > 6000) return NextResponse.json({ success: false, error: { code: "paper_question_invalid", message: "Question must contain 1 to 6000 characters" } }, { status: 400 })
  const result = await safeApiPost(`/api/v1/research/papers/${encodeURIComponent(guard.paper.id)}/ask`, {
    question,
    locale,
  }, { signal: AbortSignal.any([request.signal, AbortSignal.timeout(55000)]) })
  if (result.ok) {
    const parsed = answerSchema.safeParse(result.data)
    if (!parsed.success || (parsed.data.metadata?.paperId && parsed.data.metadata.paperId !== guard.paper.id)) return NextResponse.json({ success: false, error: { code: "reader_answer_invalid", message: "Research answer payload is invalid" } }, { status: 502 })
    return NextResponse.json({ success: true, data: { answer: {
      paperId: guard.paper.id, locale, question, answer: parsed.data.answer,
      citations: [...new Set(parsed.data.evidenceRefs)].map((id) => ({ id, label: id, sourceType: "evidence", evidenceId: id })),
      confidence: parsed.data.confidence, generatedAt: new Date().toISOString(), cached: false,
    } } })
  }
  return NextResponse.json(
    {
      success: false,
      error: {
        code: result.errorCode,
        message: result.errorMessage,
        requestId: result.requestId,
        retryable: result.errorCode !== "paper_question_invalid" && result.errorCode !== "paper_not_found",
      },
    },
    { status: result.errorCode === "paper_not_found" ? 404 : result.errorCode === "paper_question_invalid" ? 400 : 502 }
  )
}
