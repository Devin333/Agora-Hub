import { Suspense } from "react"
import { LoginForm } from "@/app/login/login-form"
import { AuthFrame, authFrameLayout } from "@/components/auth/auth-frame"
import { defaultPostLoginPath } from "@/lib/frontend-surface"

export const dynamic = "force-dynamic"

export default function LoginPage() {
  const defaultNextPath = defaultPostLoginPath()

  return (
    <Suspense fallback={<LoginShell />}>
      <LoginForm defaultNextPath={defaultNextPath} />
    </Suspense>
  )
}

function LoginShell() {
  return (
    <AuthFrame title={<span className="inline-block h-10 w-64 rounded-xl bg-muted" />} layout={authFrameLayout()}>
      <div className="h-12 w-full rounded-xl bg-muted" />
      <div className="mt-4 h-12 w-full rounded-xl bg-muted" />
      <div className="mt-6 h-12 w-full rounded-xl bg-muted" />
    </AuthFrame>
  )
}
