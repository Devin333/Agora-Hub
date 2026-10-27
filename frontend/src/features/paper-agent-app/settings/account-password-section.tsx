"use client"

import { useLayoutEffect, useState, type FormEvent } from "react"
import { KeyRound, LoaderCircle } from "lucide-react"
import { AccountServiceError, changeOwnPassword } from "@/lib/account/client"
import { cx, InlineError } from "./settings-ui"
import styles from "../paper-agent-settings-panel.module.css"

const emptyDraft = { current: "", next: "", confirm: "" }

/** Changes the signed-in account's password; other sessions of the account end. */
export function AccountPasswordSection({ onDraftChange }: { onDraftChange: (dirty: boolean) => void }) {
  const [draft, setDraft] = useState(emptyDraft)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const dirty = Boolean(draft.current || draft.next || draft.confirm)

  useLayoutEffect(() => { onDraftChange(dirty) }, [dirty, onDraftChange])

  function update(field: keyof typeof emptyDraft, value: string) {
    setDraft(previous => ({ ...previous, [field]: value }))
    setError(""); setNotice("")
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    const problem = passwordProblem(draft)
    if (problem) { setError(problem); return }
    setBusy(true); setError(""); setNotice("")
    try {
      await changeOwnPassword(draft.current, draft.next)
      setDraft(emptyDraft)
      setNotice("密码已修改。其他设备上的登录已失效，需要用新密码重新登录。")
    } catch (cause) {
      setError(cause instanceof AccountServiceError ? cause.message : "密码修改失败，请稍后重试。")
    } finally {
      setBusy(false)
    }
  }

  return <form className={styles.configEditor} onSubmit={event => void submit(event)} noValidate>
    <fieldset className={styles.configFieldset} disabled={busy}>
      <legend>修改密码</legend>
      <div className={styles.formGrid}>
        <label className={styles.wideField}><span>当前密码</span><input type="password" autoComplete="current-password" maxLength={256} value={draft.current} onChange={event => update("current", event.target.value)} /></label>
        <label><span>新密码（至少 8 位）</span><input type="password" autoComplete="new-password" minLength={8} maxLength={256} value={draft.next} onChange={event => update("next", event.target.value)} /></label>
        <label><span>再次输入新密码</span><input type="password" autoComplete="new-password" minLength={8} maxLength={256} value={draft.confirm} onChange={event => update("confirm", event.target.value)} /></label>
      </div>
    </fieldset>
    <div className={styles.configActions}>
      <button type="submit" className={styles.primaryButton} disabled={busy || !dirty}>{busy ? <LoaderCircle className={styles.spinner} size={16} /> : <KeyRound size={16} />}修改密码</button>
    </div>
    {error && <InlineError>{error}</InlineError>}
    {notice && <p className={cx(styles.resultLine, styles.success)} role="status">{notice}</p>}
  </form>
}

export function passwordProblem(draft: { current: string; next: string; confirm: string }): string | null {
  if (!draft.current) return "请输入当前密码。"
  if (draft.next.length < 8) return "新密码至少需要 8 位。"
  if (draft.next.length > 256) return "新密码不能超过 256 位。"
  if (draft.next !== draft.confirm) return "两次输入的新密码不一致。"
  if (draft.next === draft.current) return "新密码不能与当前密码相同。"
  return null
}
