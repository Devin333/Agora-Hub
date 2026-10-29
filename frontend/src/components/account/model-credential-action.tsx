"use client"

import { KeyRound } from "lucide-react"
import { Button } from "@/components/ui/button"
import { isModelCredentialFailure, openSettings } from "@/lib/account/settings-navigation"

/**
 * Shown next to a failure the user fixes with their own model API key:
 * renders nothing for every other failure code.
 */
export function ModelCredentialAction({ code, className }: { code: string | null | undefined; className?: string }) {
  if (!isModelCredentialFailure(code)) return null
  return (
    <Button type="button" variant="outline" size="sm" className={className} onClick={() => openSettings("model")}>
      <KeyRound aria-hidden="true" />
      {code === "model_credential_required" ? "填写模型 API Key" : "重新填写模型 API Key"}
    </Button>
  )
}
