import { cn } from "@/lib/utils"

/**
 * Agora mark in the rakazo idiom: a muted disc with two primary-ink "eyes".
 * Proportions follow rakazo's 74px auth logo (9px x 20px eyes), scaled to `size`.
 */
export function BrandMark({ size = 32, className }: { size?: number; className?: string }) {
  const eyeWidth = Math.max(3, Math.round(size * 0.12))
  const eyeHeight = Math.round(size * 0.27)
  return (
    <span
      aria-hidden="true"
      data-slot="brand-mark"
      className={cn("flex shrink-0 items-center justify-center rounded-full bg-muted", className)}
      style={{ width: size, height: size, gap: Math.round(size * 0.1) }}
    >
      <span className="rounded-full bg-primary" style={{ width: eyeWidth, height: eyeHeight }} />
      <span className="rounded-full bg-primary" style={{ width: eyeWidth, height: eyeHeight }} />
    </span>
  )
}
