"use client"

import Link from "next/link"
import { AlertTriangle, Loader2, SearchX } from "lucide-react"
import { Button } from "@/components/ui/button"
import type { StudioPageStateAction, StudioPageStateKind } from "@/types/studio"

export function StudioPageState({
  kind,
  title,
  description,
  action
}: {
  kind: StudioPageStateKind
  title: string
  description?: string
  action?: StudioPageStateAction
}) {
  const Icon = kind === "loading" ? Loader2 : kind === "error" ? AlertTriangle : SearchX
  const tone = kind === "error" ? "text-destructive" : "text-muted-foreground"

  return (
    <section className="flex min-h-48 flex-col items-center justify-center rounded-md border border-dashed border-border bg-card/70 px-6 py-10 text-center">
      <Icon className={`mb-3 size-8 ${tone} ${kind === "loading" ? "animate-spin" : ""}`} />
      <h2 className="text-sm font-semibold text-foreground">{title}</h2>
      {description ? <p className="mt-2 max-w-xl text-sm leading-6 text-muted-foreground">{description}</p> : null}
      {action ? <StudioPageStateActionButton action={action} /> : null}
    </section>
  )
}

function StudioPageStateActionButton({ action }: { action: StudioPageStateAction }) {
  if (action.href) {
    return (
      <Link
        href={action.href}
        className="mt-4 inline-flex h-9 items-center justify-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
      >
        {action.label}
      </Link>
    )
  }

  return (
    <Button className="mt-4" variant="outline" onClick={action.onClick}>
      {action.label}
    </Button>
  )
}
