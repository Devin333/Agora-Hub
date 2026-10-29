import { Badge as UiBadge, type BadgeVariant } from "@/components/ui/badge"

const toneVariant = {
  neutral: "default",
  success: "success",
  warning: "warning",
  danger: "danger",
  info: "info",
  accent: "accent"
} satisfies Record<string, BadgeVariant>

/** Tone-named wrapper kept for the evidence and quality badges built on it. */
export function Badge({
  children,
  tone = "neutral",
  className
}: {
  children: React.ReactNode
  tone?: keyof typeof toneVariant
  className?: string
}) {
  return (
    <UiBadge variant={toneVariant[tone]} className={className}>
      {children}
    </UiBadge>
  )
}
