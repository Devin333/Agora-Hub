"use client"

import { useCallback, useEffect, useLayoutEffect, useState, type FormEvent } from "react"
import { Copy, LoaderCircle, RefreshCw, UserPlus } from "lucide-react"
import {
  AccountServiceError,
  changeAccountRole,
  createAccount,
  listAccounts,
  resetAccountPassword,
  setAccountEnabled,
  type Account,
  type AccountRole,
} from "@/lib/account/client"
import { notifyPortalSessionChanged } from "@/lib/auth/session-events"
import { cx, formatTime, InlineError, LoadingLine, StatusBadge } from "./settings-ui"
import styles from "../paper-agent-settings-panel.module.css"

const roleLabels: Record<AccountRole, string> = { admin: "管理员", user: "普通用户" }
type IssuedPassword = { username: string; password: string; reason: "created" | "reset" }

/** Administrator-only account management: create, disable, reset and change roles. */
export function AccountAdminSection({ currentUserId, onDraftChange }: { currentUserId: string; onDraftChange: (dirty: boolean) => void }) {
  const [accounts, setAccounts] = useState<Account[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState("")
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const [issued, setIssued] = useState<IssuedPassword | null>(null)
  const [username, setUsername] = useState("")
  const [role, setRole] = useState<AccountRole>("user")

  // A generated password is shown exactly once, so leaving it unread counts as a draft.
  useLayoutEffect(() => { onDraftChange(Boolean(username.trim()) || issued !== null) }, [issued, onDraftChange, username])

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    try {
      const next = await listAccounts(signal)
      if (!signal?.aborted) setAccounts(sortAccounts(next))
    } catch (cause) {
      if (!signal?.aborted) setError(messageOf(cause, "无法读取账号列表。"))
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  function replaceAccount(account: Account) {
    setAccounts(previous => sortAccounts((previous ?? []).map(item => item.userId === account.userId ? account : item)))
  }

  async function act(id: string, action: () => Promise<void>) {
    if (busyId) return
    setBusyId(id); setError(""); setNotice("")
    try { await action() }
    catch (cause) { setError(messageOf(cause, "操作未完成，请稍后重试。")) }
    finally { setBusyId("") }
  }

  function submitCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const name = username.trim()
    if (name.length < 3 || name.length > 64) { setError("用户名需要 3–64 个字符。"); return }
    void act("create", async () => {
      const result = await createAccount({ username: name, role })
      setAccounts(previous => sortAccounts([...(previous ?? []), result.account]))
      setUsername(""); setRole("user")
      if (result.initialPassword) setIssued({ username: result.account.username, password: result.initialPassword, reason: "created" })
      setNotice(`已创建账号「${result.account.username}」。`)
    })
  }

  function toggleEnabled(account: Account) {
    const verb = account.enabled ? "停用" : "启用"
    if (account.enabled && !window.confirm(`停用「${account.username}」？该账号的所有登录会立即失效，数据保留。`)) return
    void act(account.userId, async () => {
      replaceAccount(await setAccountEnabled(account.userId, !account.enabled))
      setNotice(`已${verb}「${account.username}」。`)
    })
  }

  function resetPassword(account: Account) {
    if (!window.confirm(`为「${account.username}」生成新的初始密码？该账号的所有登录会立即失效。`)) return
    void act(account.userId, async () => {
      const result = await resetAccountPassword(account.userId)
      replaceAccount(result.account)
      if (result.initialPassword) setIssued({ username: result.account.username, password: result.initialPassword, reason: "reset" })
      setNotice(`已重置「${account.username}」的密码。`)
    })
  }

  function changeRole(account: Account, next: AccountRole) {
    if (next === account.role) return
    const self = account.userId === currentUserId
    const warning = self
      ? "把自己改为普通用户后，你会失去账号管理权限，并需要重新登录。确定继续？"
      : `把「${account.username}」改为${roleLabels[next]}？该账号需要重新登录。`
    if (!window.confirm(warning)) return
    void act(account.userId, async () => {
      replaceAccount(await changeAccountRole(account.userId, next))
      setNotice(`「${account.username}」现在是${roleLabels[next]}。`)
      // A role change ends the account's sessions, including this one.
      if (self) notifyPortalSessionChanged()
    })
  }

  return <div className={styles.configEditor}>
    <form onSubmit={submitCreate} noValidate>
      <fieldset className={styles.configFieldset} disabled={busyId !== ""}>
        <legend>新建账号</legend>
        <div className={styles.formGrid}>
          <label><span>用户名</span><input value={username} minLength={3} maxLength={64} autoComplete="off" spellCheck={false} onChange={event => { setUsername(event.target.value); setError("") }} /></label>
          <label><span>角色</span><select value={role} onChange={event => setRole(event.target.value as AccountRole)}><option value="user">普通用户</option><option value="admin">管理员</option></select></label>
        </div>
        <div className={styles.configActions}>
          <button type="submit" className={styles.primaryButton} disabled={busyId !== "" || !username.trim()}>{busyId === "create" ? <LoaderCircle className={styles.spinner} size={16} /> : <UserPlus size={16} />}创建并生成初始密码</button>
        </div>
      </fieldset>
    </form>

    {issued && <IssuedPasswordNotice issued={issued} onDismiss={() => setIssued(null)} />}
    {error && <InlineError>{error}</InlineError>}
    {notice && <p className={cx(styles.resultLine, styles.success)} role="status">{notice}</p>}

    <div className={styles.configStatus}>
      <div><strong>全部账号</strong><span>{accounts ? `共 ${accounts.length} 个，其中 ${accounts.filter(item => item.enabled).length} 个启用中` : "正在读取"}</span></div>
      <button type="button" className={styles.secondaryButton} disabled={loading || busyId !== ""} onClick={() => { setError(""); void load() }}>{loading ? <LoaderCircle className={styles.spinner} size={16} /> : <RefreshCw size={16} />}刷新</button>
    </div>
    {loading && !accounts ? <LoadingLine /> : accounts && <ul className={styles.accountList}>{accounts.map(account => {
      const self = account.userId === currentUserId
      const busy = busyId === account.userId
      return <li key={account.userId}>
        <div className={styles.accountMain}>
          <span className={styles.capabilityName}>{account.username}{self ? "（你）" : ""}</span>
          <StatusBadge tone={account.enabled ? "success" : "neutral"}>{account.enabled ? "启用中" : "已停用"}</StatusBadge>
          <select aria-label={`角色：${account.username}`} value={account.role} disabled={busyId !== ""} onChange={event => changeRole(account, event.target.value as AccountRole)}>
            <option value="user">普通用户</option><option value="admin">管理员</option>
          </select>
        </div>
        <p>创建于 {formatTime(account.createdAt)}{account.passwordChangedAt ? ` · 密码更新于 ${formatTime(account.passwordChangedAt)}` : ""}{account.disabledAt ? ` · 停用于 ${formatTime(account.disabledAt)}` : ""}</p>
        <div className={styles.maintenanceActions}>
          <button type="button" className={styles.secondaryButton} disabled={busyId !== ""} onClick={() => resetPassword(account)}>{busy ? <LoaderCircle className={styles.spinner} size={14} /> : null}重置密码</button>
          <button type="button" className={styles.secondaryButton} disabled={busyId !== "" || (self && account.enabled)} title={self && account.enabled ? "不能停用自己正在使用的账号" : undefined} onClick={() => toggleEnabled(account)}>{account.enabled ? "停用" : "启用"}</button>
        </div>
      </li>
    })}</ul>}
  </div>
}

function IssuedPasswordNotice({ issued, onDismiss }: { issued: IssuedPassword; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try { await navigator.clipboard.writeText(issued.password); setCopied(true) }
    catch { setCopied(false) }
  }
  return <div className={styles.issuedPassword} role="status">
    <div>
      <strong>「{issued.username}」的{issued.reason === "created" ? "初始" : "新"}密码</strong>
      <p>只显示这一次，关闭后无法再查看。请通过安全渠道交给对方，并提醒对方登录后在设置里修改密码。</p>
      <code>{issued.password}</code>
    </div>
    <div className={styles.maintenanceActions}>
      <button type="button" className={styles.secondaryButton} onClick={() => void copy()}><Copy size={14} />{copied ? "已复制" : "复制"}</button>
      <button type="button" className={styles.primaryButton} onClick={onDismiss}>我已记下</button>
    </div>
  </div>
}

function sortAccounts(accounts: Account[]): Account[] {
  return [...accounts].sort((left, right) => Number(right.enabled) - Number(left.enabled) || left.username.localeCompare(right.username, "zh-CN"))
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof AccountServiceError ? cause.message : fallback
}
