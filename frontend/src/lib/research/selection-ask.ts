import type { ReaderSelectionAskAnswer } from "@/components/papers/open-reader/open-reader-types"

export function matchesSelectionAnswerScope(
  answer: ReaderSelectionAskAnswer,
  owner: string | null | undefined,
  sourceRevision: string | undefined,
  requestKey?: string,
): boolean {
  return Boolean(
    owner &&
    sourceRevision &&
    answer.owner === owner &&
    answer.sourceRevision === sourceRevision &&
    (!requestKey || answer.requestKey === requestKey),
  )
}
