import { cn } from "@/lib/utils"

const toneClass = {
  neutral: "border-border bg-secondary text-secondary-foreground",
  success: "border-success/30 bg-success/10 text-success",
  warning: "border-warning/30 bg-warning/10 text-warning",
  danger: "border-destructive/30 bg-destructive/10 text-destructive",
  info: "border-link/30 bg-link/10 text-link",
  accent: "border-link/30 bg-link/10 text-link"
}

export function Badge({
  children,
  tone = "neutral",
  className
}: {
  children: React.ReactNode
  tone?: keyof typeof toneClass
  className?: string
}) {
  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center rounded-md border px-2 py-1 text-xs font-medium leading-none",
        toneClass[tone],
        className
      )}
    >
      {children}
    </span>
  )
}
