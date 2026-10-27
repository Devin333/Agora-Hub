"use client"

import Link from "next/link"
import { Loader2 } from "lucide-react"
import { Button, buttonVariants } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type { StudioPageStateAction, StudioPageStateKind } from "@/types/studio"

/** Rakazo-style pane state: plain muted copy, destructive tint only for errors. */
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
  return (
    <section
      role={kind === "error" ? "alert" : undefined}
      aria-busy={kind === "loading" ? true : undefined}
      className="grid min-h-48 place-items-center px-6 py-10 text-center"
    >
      <div className="flex flex-col items-center">
        {kind === "loading" ? <Loader2 size={18} strokeWidth={1.8} className="mb-3 animate-spin text-muted-foreground" aria-hidden="true" /> : null}
        <h2 className={`text-body2 font-medium ${kind === "error" ? "text-destructive-text" : "text-muted-foreground"}`}>{title}</h2>
        {description ? <p className="mt-1 max-w-xl text-body2 text-muted-foreground/80">{description}</p> : null}
        {action ? <StudioPageStateActionButton action={action} /> : null}
      </div>
    </section>
  )
}

function StudioPageStateActionButton({ action }: { action: StudioPageStateAction }) {
  if (action.href) {
    return (
      <Link href={action.href} className={cn(buttonVariants({ variant: "default", size: "lg" }), "mt-4 px-4")}>
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
