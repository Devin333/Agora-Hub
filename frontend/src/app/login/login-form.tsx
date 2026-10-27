"use client"

import { FormEvent, useEffect, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { AuthField, AuthFrame, authFrameLayout } from "@/components/auth/auth-frame"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { bootstrapAccount, fetchAuthSession, login } from "@/lib/auth/api"
import { useI18n } from "@/lib/i18n/use-i18n"
import { safeLocalNavigation } from "@/lib/research/routes"

export function LoginForm({ defaultNextPath = "/papers" }: { defaultNextPath?: string }) {
  const router = useRouter()
  const searchParams = useSearchParams()
  const { t } = useI18n()
  const nextPath = safeNextPath(searchParams.get("next"), defaultNextPath)
  const [initialized, setInitialized] = useState<boolean | null>(null)
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [status, setStatus] = useState<"idle" | "loading" | "success" | "error">("idle")
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchAuthSession()
      .then((result) => {
        if (cancelled) return
        setInitialized(result.initialized)
        if (result.session) {
          router.replace(nextPath)
        }
      })
      .catch((requestError) => {
        if (!cancelled) {
          setInitialized(true)
          setError(requestError instanceof Error ? requestError.message : t("auth.sessionCheckFailed"))
        }
      })
    return () => {
      cancelled = true
    }
  }, [nextPath, router, t])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setStatus("loading")
    setError(null)
    try {
      if (initialized === false) {
        await bootstrapAccount(username, password)
      } else {
        await login(username, password)
      }
      setStatus("success")
      router.replace(nextPath)
      router.refresh()
    } catch (requestError) {
      setStatus("error")
      setError(requestError instanceof Error ? requestError.message : t("auth.loginFailed"))
    }
  }

  const isBootstrap = initialized === false
  const title = isBootstrap ? t("auth.bootstrap.title") : t("auth.login.title")

  return (
    <AuthFrame
      title={title}
      description={isBootstrap ? t("auth.bootstrap.description") : t("auth.login.description")}
      layout={authFrameLayout()}
      onSubmit={submit}
    >
      <AuthField id="login-username" label={t("auth.username")}>
        <Input
          id="login-username"
          name="username"
          size="xl"
          className="mt-2"
          autoComplete="username"
          minLength={3}
          maxLength={64}
          required
          value={username}
          onChange={(event) => setUsername(event.target.value)}
        />
      </AuthField>
      <AuthField id="login-password" label={t("auth.password")} className="mt-4">
        <Input
          id="login-password"
          name="password"
          size="xl"
          className="mt-2"
          autoComplete={isBootstrap ? "new-password" : "current-password"}
          minLength={8}
          maxLength={256}
          required
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </AuthField>
      {error ? (
        <p role="alert" className="mt-3 w-full text-sm text-destructive-text">
          {error}
        </p>
      ) : null}
      <Button type="submit" size="xl" className="mt-6 w-full" disabled={status === "loading" || initialized === null}>
        {status === "loading" ? t("auth.working") : isBootstrap ? t("auth.createAccount") : t("auth.signIn")}
      </Button>
    </AuthFrame>
  )
}

function safeNextPath(value: string | null, fallback: string) {
  if (!value || !safeLocalNavigation(value) || value.startsWith("/login") || value.startsWith("/api/")) {
    return fallback
  }
  return value
}
