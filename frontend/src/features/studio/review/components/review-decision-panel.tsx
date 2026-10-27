"use client"

import { useState } from "react"
import { CheckCircle2, ShieldAlert, XCircle } from "lucide-react"
import { ErrorState } from "@/components/common/error-state"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { useReviewActions } from "@/features/studio/review/hooks/use-review-actions"
import { StudioNotice, StudioPanel } from "@/features/studio/shared/components/studio-dashboard"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { ReviewActionRequest, ReviewActionResult, ReviewDecisionAction, StudioReviewItem } from "@/types/review"

export type ReviewDecisionHandler = (request: ReviewActionRequest) => Promise<ReviewActionResult>

export function ReviewDecisionPanel({
  item,
  onSubmitAction
}: {
  item: StudioReviewItem
  onSubmitAction?: ReviewDecisionHandler
}) {
  const { locale, t } = useI18n()
  const { submitAction } = useReviewActions()
  const [error, setError] = useState<{ title?: string; message: string } | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [pendingRequest, setPendingRequest] = useState<ReviewActionRequest | null>(null)
  const disabledReason = actionDisabledReason(item, t)
  const handler = onSubmitAction ?? submitAction

  function beginAction(action: ReviewDecisionAction) {
    setError(null)
    setSuccess(null)
    const request = buildActionRequest(action)
    if ("error" in request) {
      setError({ message: request.error })
      return
    }
    setPendingRequest(request)
  }

  async function confirmAction() {
    if (!pendingRequest) return
    setIsSubmitting(true)
    setError(null)
    setSuccess(null)
    const result = await handler(pendingRequest)
    setIsSubmitting(false)
    setPendingRequest(null)

    if (result.ok) {
      setSuccess(result.requestId ? t("studio.review.actionRecordedRequest", { requestId: result.requestId }) : t("studio.review.actionRecorded"))
      return
    }

    setError({
      title: result.errorCode,
      message: result.requestId ? `${result.errorMessage} Request ${result.requestId}.` : result.errorMessage
    })
  }

  function buildActionRequest(action: ReviewDecisionAction): ReviewActionRequest | { error: string } {
    if (disabledReason) return { error: disabledReason }
    return { item, action }
  }

  const showApprovalButtons = item.actionKind === "approval_decision"

  return (
    <StudioPanel
      title={t("studio.review.decision")}
      description={disabledReason ?? decisionSummary(item, t)}
      actions={<ShieldAlert size={16} strokeWidth={1.8} aria-hidden="true" className="text-muted-foreground" />}
      className="h-fit"
      contentClassName="space-y-3"
    >

      {error ? <ErrorState title={error.title ?? t("studio.review.actionFailed")} message={error.message} /> : null}
      {success ? (
        <div role="status">
          <StudioNotice tone="success">{success}</StudioNotice>
        </div>
      ) : null}

      <div className="flex flex-wrap gap-2">
        {showApprovalButtons ? (
          <>
            <Button disabled={Boolean(disabledReason) || isSubmitting} onClick={() => beginAction("approve")} type="button">
              <CheckCircle2 size={15} strokeWidth={1.8} aria-hidden="true" />
              {t("studio.review.approve")}
            </Button>
            <Button disabled={Boolean(disabledReason) || isSubmitting} onClick={() => beginAction("reject")} type="button" variant="destructive">
              <XCircle size={15} strokeWidth={1.8} aria-hidden="true" />
              {t("studio.review.reject")}
            </Button>
          </>
        ) : null}
      </div>

      <Dialog open={Boolean(pendingRequest)} onOpenChange={(open) => !open && setPendingRequest(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("studio.review.confirmAction", { action: pendingRequest ? actionLabel(pendingRequest.action, locale) : t("common.actions") })}</DialogTitle>
            <DialogDescription>
              {t("studio.review.confirmDescription", { approvalId: item.approvalId })}
            </DialogDescription>
          </DialogHeader>
          <div className="mt-2 flex justify-end gap-2">
            <Button disabled={isSubmitting} onClick={() => setPendingRequest(null)} type="button" variant="outline">
              {t("common.cancel")}
            </Button>
            <Button disabled={isSubmitting} onClick={confirmAction} type="button">
              {t("studio.review.confirm", { action: pendingRequest ? actionLabel(pendingRequest.action, locale) : t("common.actions") })}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </StudioPanel>
  )
}

function actionDisabledReason(item: StudioReviewItem, t: ReturnType<typeof useI18n>["t"]): string | undefined {
  if (item.actionDisabledReason) return item.actionDisabledReason
  if (item.actionKind === "none") return t("studio.review.noOperationAvailable")
  if (item.status !== "pending") return t("studio.review.alreadyDecided")
  return undefined
}

function decisionSummary(item: StudioReviewItem, t: ReturnType<typeof useI18n>["t"]): string {
  return t("studio.review.pendingSummary")
}

function actionLabel(action: ReviewDecisionAction, locale: "zh" | "en"): string {
  if (locale === "zh") {
    if (action === "approve") return "通过"
    if (action === "reject") return "驳回"
  }
  return action
}
