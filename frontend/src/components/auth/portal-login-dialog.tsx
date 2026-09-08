"use client"

import * as Dialog from "@radix-ui/react-dialog"
import * as Tabs from "@radix-ui/react-tabs"
import { ArrowLeft, ArrowUpRight, Check, LoaderCircle, Mail, RefreshCw, ShieldCheck, Smartphone, WandSparkles, X } from "lucide-react"
import { useEffect, useRef, useState, type FormEvent } from "react"
import { authErrorMessage, authorizeProvider, fetchLoginMethods, fetchPortalSession, requestOtp, verifyOtp, type AuthMethods, type OAuthProvider, type OtpChallenge, type OtpChannel } from "@/lib/auth/portal-api"
import type { AuthSession } from "@/lib/papers/types"
import { ProviderIcon } from "./provider-icons"
import styles from "./portal-login.module.css"
import { preserveResearchForRedirect } from "@/lib/auth/research-return-context"

type Props = { open: boolean; onOpenChange: (open: boolean) => void; onAuthenticated: (session: AuthSession) => void; onRestoreFocus: () => void }
type Authorization = { provider: OAuthProvider; url: string; state: string; expiresAt: string; blocked: boolean }
type Draft = { value: string; code: string; country: string; challenge: OtpChallenge | null; sentTo: string }
const emptyDraft = (): Draft => ({ value: "", code: "", country: "+86", challenge: null, sentTo: "" })
const providerNames = { google: "Google", wechat: "微信", qq: "QQ" }
const providerHosts = { google: "accounts.google.com", wechat: "open.weixin.qq.com", qq: "graph.qq.com" }

export function validAuthorizationUrl(provider: OAuthProvider, value: string) {
  try { const url = new URL(value); return url.protocol === "https:" && url.hostname === providerHosts[provider] && !url.username && !url.password && Boolean(url.searchParams.get("state")) } catch { return false }
}

export function PortalLoginDialog({ open, onOpenChange, onAuthenticated, onRestoreFocus }: Props) {
  const [channel, setChannel] = useState<OtpChannel>("phone")
  const [drafts, setDrafts] = useState<Record<OtpChannel, Draft>>({ phone: emptyDraft(), email: emptyDraft() })
  const [methods, setMethods] = useState<AuthMethods | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState<"send" | "verify" | "oauth" | null>(null)
  const [error, setError] = useState("")
  const [consent, setConsent] = useState(false)
  const [auth, setAuth] = useState<Authorization | null>(null)
  const [now, setNow] = useState(Date.now())
  const [retry, setRetry] = useState(0)
  const [retryUntil, setRetryUntil] = useState(0)
  const request = useRef<AbortController | null>(null)
  const popup = useRef<Window | null>(null)
  const frame = useRef<HTMLIFrameElement>(null)
  const codeInput = useRef<HTMLInputElement>(null)
  const input = useRef<HTMLInputElement>(null)
  const draft = drafts[channel]
  const destination = channel === "phone" ? `${draft.country}${draft.value.replace(/[\s()-]/g, "")}` : draft.value.trim()
  const ready = Boolean(methods?.methods[channel]?.available && methods.termsUrl && methods.privacyUrl)
  const remaining = Math.max(0, Math.ceil((Math.max(Date.parse(draft.challenge?.resendAt ?? "") || 0, retryUntil) - now) / 1000))
  const expired = Boolean(draft.challenge && Date.parse(draft.challenge.expiresAt) <= now)
  const bound = draft.sentTo === destination && !expired

  function updateDraft(patch: Partial<Draft>, target = channel) { setDrafts((current) => ({ ...current, [target]: { ...current[target], ...patch } })) }

  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    setLoading(true)
    setError("")
    fetchLoginMethods(controller.signal).then(setMethods).catch((cause) => { if (!controller.signal.aborted) setError(authErrorMessage(cause)) }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [open, retry])

  useEffect(() => {
    if (!open) return
    const interval = window.setInterval(() => setNow(Date.now()), 1000)
    return () => {
      window.clearInterval(interval)
      request.current?.abort()
      request.current = null
      popup.current?.close()
      popup.current = null
      setBusy(null)
      setAuth(null)
      setDrafts({ phone: emptyDraft(), email: emptyDraft() })
      setConsent(false)
      setMethods(null)
    }
  }, [open])

  useEffect(() => {
    if (!open || !auth) return
    const controller = new AbortController()
    async function complete(event: MessageEvent) {
      const data = event.data
      if (event.origin !== window.location.origin || !event.source || (event.source !== popup.current && event.source !== frame.current?.contentWindow) || data?.type !== "agora-auth-complete" || data?.state !== auth!.state || data?.provider !== auth!.provider) return
      if (!data.success) { setError("授权未完成，请重试或选择其他方式。"); setBusy(null); return }
      setBusy("oauth")
      try {
        const result = await fetchPortalSession(controller.signal)
        if (!result.session) throw new Error("No session")
        if (!controller.signal.aborted) onAuthenticated(result.session)
      } catch (cause) { if (!controller.signal.aborted) { setError(authErrorMessage(cause)); setBusy(null) } }
    }
    window.addEventListener("message", complete)
    const interval = window.setInterval(() => {
      if (popup.current?.closed) { popup.current = null; setBusy(null); setError("授权窗口已关闭，可重新打开或选择其他方式。") }
    }, 750)
    return () => { controller.abort(); window.removeEventListener("message", complete); window.clearInterval(interval) }
  }, [open, auth, onAuthenticated])

  function validateDestination() {
    const valid = channel === "phone" ? /^\+[1-9]\d{6,14}$/.test(destination) : /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(destination)
    if (!valid) { setError(channel === "phone" ? "请输入有效的手机号。" : "请输入有效的邮箱地址。"); input.current?.focus(); return false }
    if (!consent) { setError("请先阅读并同意用户协议和隐私政策。"); return false }
    return true
  }

  async function send() {
    if (busy || !ready || remaining || !validateDestination()) return
    const controller = new AbortController()
    request.current = controller
    setBusy("send"); setError("")
    try {
      const challenge = await requestOtp(channel, destination, controller.signal)
      if (controller.signal.aborted) return
      updateDraft({ challenge, sentTo: destination, code: "" })
      setNow(Date.now())
      codeInput.current?.focus()
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(authErrorMessage(cause))
        if (cause && typeof cause === "object" && "retryAfter" in cause) setRetryUntil(Date.now() + Number(cause.retryAfter) * 1000)
      }
    } finally { if (!controller.signal.aborted) setBusy(null) }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (busy || !ready || !validateDestination()) return
    if (!draft.challenge || !bound) { setError("请先为当前账号获取验证码。"); return }
    if (!/^\d{6}$/.test(draft.code)) { setError("请输入完整的 6 位验证码。"); codeInput.current?.focus(); return }
    const controller = new AbortController()
    request.current = controller
    setBusy("verify"); setError("")
    try {
      const result = await verifyOtp(draft.challenge.challengeId, draft.code, controller.signal)
      if (!controller.signal.aborted) onAuthenticated(result.session)
    } catch (cause) { if (!controller.signal.aborted) setError(authErrorMessage(cause)) }
    finally { if (!controller.signal.aborted) setBusy(null) }
  }

  async function startProvider(provider: OAuthProvider) {
    setError("")
    if (!methods?.methods[provider]?.available) { setError(`${providerNames[provider]}登录暂未开放，请选择其他方式。`); return }
    if (!consent) { setError("请先阅读并同意用户协议和隐私政策。"); return }
    if (busy) return
    popup.current?.close()
    popup.current = provider === "wechat" ? null : window.open("about:blank", `agora-auth-${Date.now()}`, "popup,width=520,height=680")
    const controller = new AbortController()
    request.current = controller
    setBusy("oauth")
    try {
      const result = await authorizeProvider(provider, `${window.location.pathname}${window.location.search}${window.location.hash}`, controller.signal)
      if (controller.signal.aborted) return
      if (!validAuthorizationUrl(provider, result.authorizationUrl)) throw new Error("Invalid provider URL")
      const url = new URL(result.authorizationUrl)
      if (provider === "wechat") url.searchParams.set("self_redirect", "true")
      setAuth({ provider, url: url.toString(), state: url.searchParams.get("state")!, expiresAt: result.expiresAt, blocked: provider !== "wechat" && !popup.current })
      if (popup.current) popup.current.location.href = url.toString()
    } catch (cause) { popup.current?.close(); if (!controller.signal.aborted) setError(authErrorMessage(cause)) }
    finally { if (!controller.signal.aborted) setBusy(null) }
  }

  function back() { request.current?.abort(); popup.current?.close(); popup.current = null; setAuth(null); setBusy(null); setError("") }
  const authExpired = auth && Date.parse(auth.expiresAt) <= now

  return <Dialog.Root open={open} onOpenChange={onOpenChange}>
    <Dialog.Portal><Dialog.Overlay className={styles.overlay} /><Dialog.Content className={styles.dialog} onCloseAutoFocus={(event) => { event.preventDefault(); onRestoreFocus() }} onOpenAutoFocus={(event) => { event.preventDefault(); input.current?.focus() }}>
      <Dialog.Close className={styles.close} aria-label="关闭登录" title="关闭登录"><X size={21} /></Dialog.Close>
      <div className={styles.heading}><div className={styles.brandMark}><WandSparkles size={23} /></div><Dialog.Title className={styles.title}>{auth ? `${providerNames[auth.provider]}登录` : <>登录 Agora<span>AI</span></>}</Dialog.Title></div>
      <Dialog.Description className={styles.subtitle}>{auth ? (auth.provider === "wechat" ? "使用微信扫码，在手机上确认登录" : `在 ${providerNames[auth.provider]} 窗口中完成授权`) : "继续你的下一次发现。"}</Dialog.Description>
      {auth ? <div className={styles.authorization}>
        {authExpired ? <div className={styles.providerStatus}><RefreshCw size={30} /><p>本次授权已过期</p><button type="button" className={styles.primary} onClick={() => void startProvider(auth.provider)}>重新获取</button></div>
          : auth.provider === "wechat" ? <iframe ref={frame} src={auth.url} title="微信官方扫码登录" className={styles.qrFrame} referrerPolicy="no-referrer" />
          : <div className={styles.providerStatus}><ProviderIcon provider={auth.provider} /><p>{auth.blocked ? "浏览器未能打开授权窗口" : "等待授权完成"}</p>{auth.blocked ? <a className={styles.primary} href={auth.url} onClick={preserveResearchForRedirect}>继续前往 {providerNames[auth.provider]}<ArrowUpRight size={17} /></a> : <button type="button" className={styles.primary} onClick={() => void startProvider(auth.provider)} disabled={Boolean(busy)}>重新打开授权窗口<ArrowUpRight size={17} /></button>}</div>}
        <button type="button" className={styles.back} onClick={back}><ArrowLeft size={16} />其他登录方式</button>
      </div> : <>
        <Tabs.Root value={channel} onValueChange={(value) => { setChannel(value as OtpChannel); setError("") }}>
          <Tabs.List className={styles.tabs} aria-label="登录方式"><Tabs.Trigger value="phone" disabled={Boolean(busy)}><Smartphone size={17} />手机号</Tabs.Trigger><Tabs.Trigger value="email" disabled={Boolean(busy)}><Mail size={17} />邮箱</Tabs.Trigger></Tabs.List>
          <form onSubmit={submit} className={styles.form} noValidate>
            <label htmlFor="portal-login-identity" className={styles.label}>{channel === "phone" ? "手机号" : "邮箱地址"}</label>
            <div className={styles.inputShell}>{channel === "phone" && <select aria-label="国家或地区代码" value={draft.country} disabled={Boolean(busy)} onChange={(event) => updateDraft({ country: event.target.value })}><option value="+86">+86</option><option value="+852">+852</option><option value="+886">+886</option><option value="+1">+1</option><option value="+44">+44</option><option value="+81">+81</option><option value="+49">+49</option><option value="+61">+61</option></select>}<input ref={input} id="portal-login-identity" type={channel === "phone" ? "tel" : "email"} autoComplete={channel === "phone" ? "tel-national" : "email"} value={draft.value} maxLength={channel === "phone" ? 24 : 254} disabled={Boolean(busy)} onChange={(event) => updateDraft({ value: event.target.value })} placeholder={channel === "phone" ? "输入手机号" : "name@example.com"} aria-describedby={error ? "portal-login-error" : undefined} /></div>
            <label htmlFor="portal-login-code" className={styles.codeLabel}>验证码</label>
            <div className={styles.inputShell}><input ref={codeInput} id="portal-login-code" type="text" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={draft.code} disabled={Boolean(busy)} onChange={(event) => updateDraft({ code: event.target.value.replace(/\D/g, "").slice(0, 6) })} placeholder="6 位验证码" aria-describedby="portal-login-status" /><button type="button" className={styles.sendCode} disabled={!ready || Boolean(busy) || remaining > 0} onClick={() => void send()}>{busy === "send" ? <LoaderCircle size={16} className={styles.spinner} /> : remaining ? `${remaining}s 后重发` : "获取验证码"}</button></div>
            <div className={styles.methodStatus} id="portal-login-status" role="status">{loading ? <><LoaderCircle size={13} className={styles.spinner} />正在连接登录服务</> : !methods ? <button type="button" onClick={() => setRetry((n) => n + 1)}><RefreshCw size={13} />重新连接登录服务</button> : !ready ? `${channel === "phone" ? "手机号" : "邮箱"}登录暂未开放` : expired ? "验证码已过期，请重新获取。" : draft.challenge && bound ? <><Check size={14} />验证码已发送{channel === "email" ? "，请检查收件箱或垃圾邮件。" : "，请查收短信。"}</> : "首次验证后将为你创建账号。"}</div>
            <button type="submit" className={styles.primary} disabled={!ready || Boolean(busy)}>{busy === "verify" ? <><LoaderCircle size={18} className={styles.spinner} />正在登录</> : "登录 / 注册"}</button>
          </form>
        </Tabs.Root>
        <div className={styles.divider}><span />其他登录方式<span /></div>
        <div className={styles.providers}>{(["google", "wechat", "qq"] as const).map((provider) => <button key={provider} type="button" disabled={loading || Boolean(busy)} aria-label={`使用 ${providerNames[provider]} 登录`} title={methods?.methods[provider]?.available ? `${providerNames[provider]}登录` : `${providerNames[provider]}登录暂未开放`} onClick={() => void startProvider(provider)}><ProviderIcon provider={provider} /><span>{providerNames[provider]}</span></button>)}</div>
        {methods?.termsUrl && methods?.privacyUrl ? <label className={styles.consent}><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} /><span>我已阅读并同意<a href={methods.termsUrl} target="_blank" rel="noopener noreferrer">用户协议</a>和<a href={methods.privacyUrl} target="_blank" rel="noopener noreferrer">隐私政策</a></span></label> : <p className={styles.footer}><ShieldCheck size={14} />安全登录，专注研究</p>}
      </>}
      {error && <p id="portal-login-error" className={styles.error} role="alert">{error}</p>}
    </Dialog.Content></Dialog.Portal>
  </Dialog.Root>
}
