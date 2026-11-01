"use client"

import { KeyRound } from "lucide-react"
import { Button } from "@/components/ui/button"
import { isModelConnectionFailure, openSettings } from "@/lib/account/settings-navigation"

// Failures fixed by entering the key again rather than by choosing a connection.
const KEY_FAILURES = new Set(["model_connection_key_missing", "model_connection_key_rotated", "model_connection_rejected"])

/**
 * Shown next to a failure the user fixes in their model connection settings:
 * renders nothing for every other failure code.
 */
export function ModelConnectionAction({ code, className }: { code: string | null | undefined; className?: string }) {
  if (!isModelConnectionFailure(code)) return null
  return (
    <Button type="button" variant="outline" size="sm" className={className} onClick={() => openSettings("model")}>
      <KeyRound aria-hidden="true" />
      {KEY_FAILURES.has(code ?? "") ? "重新填写 API Key" : "设置模型连接"}
    </Button>
  )
}
