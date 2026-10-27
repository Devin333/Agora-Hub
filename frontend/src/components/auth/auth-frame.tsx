import type { FormEvent, ReactNode } from "react"
import { BrandMark } from "@/components/brand/brand-mark"
import { isHostedProfile } from "@/lib/deployment-profile"
import { cn } from "@/lib/utils"

/**
 * `standalone` fills the viewport (desktop login runs without app chrome);
 * `embedded` leaves room for the portal header above it.
 */
export type AuthFrameLayout = "standalone" | "embedded"

/** The local desktop app renders login without app chrome, so the frame fills the viewport. */
export function authFrameLayout(): AuthFrameLayout {
  return isHostedProfile() ? "standalone" : "embedded"
}

/**
 * Rakazo auth screen: a centered 460px column with the brand mark, a large
 * title and the form body. `onSubmit` is optional so loading shells can reuse it.
 */
export function AuthFrame({
  title,
  description,
  layout = "embedded",
  onSubmit,
  children
}: {
  title: ReactNode
  description?: ReactNode
  layout?: AuthFrameLayout
  onSubmit?: (event: FormEvent<HTMLFormElement>) => void
  children?: ReactNode
}) {
  return (
    <main
      data-layout={layout}
      className={cn(
        "flex items-center justify-center bg-background px-6 py-16 text-foreground",
        layout === "standalone" ? "min-h-dvh" : "min-h-[calc(100dvh-8rem)]"
      )}
    >
      <form onSubmit={onSubmit} className="flex w-full max-w-[460px] flex-col items-center">
        <BrandMark size={74} />
        <h1
          aria-live="polite"
          className={cn("mt-7 text-center text-4xl font-medium tracking-tight", description ? "" : "mb-9")}
        >
          {title}
        </h1>
        {description ? <p className="mb-9 mt-3 text-center text-body1 text-muted-foreground">{description}</p> : null}
        {children}
      </form>
    </main>
  )
}

/** Label + field wrapper matching rakazo auth fields (muted label, full width). */
export function AuthField({
  id,
  label,
  className,
  children
}: {
  id: string
  label: ReactNode
  className?: string
  children: ReactNode
}) {
  return (
    <div className={cn("w-full", className)}>
      <label htmlFor={id} className="text-sm font-medium text-muted-foreground">
        {label}
      </label>
      {children}
    </div>
  )
}
