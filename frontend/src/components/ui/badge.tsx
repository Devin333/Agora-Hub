import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

/**
 * Rakazo pill badge. Status tones (success / warning / danger / info) keep their
 * hue as a tint so lists of states stay scannable; `default` is the quiet
 * secondary pill most metadata uses, `primary` the inverted emphasis pill.
 */
const badgeVariants = cva(
  "inline-flex h-5 w-fit max-w-full shrink-0 items-center justify-center gap-1 overflow-hidden whitespace-nowrap rounded-full border border-transparent px-2 py-0.5 text-xs font-medium transition-all [&>svg]:pointer-events-none [&>svg]:!size-3",
  {
    variants: {
      variant: {
        default: "bg-secondary text-secondary-foreground",
        primary: "bg-primary text-primary-foreground",
        outline: "border-border text-foreground",
        muted: "bg-muted text-muted-foreground",
        success: "bg-success/15 text-success",
        warning: "bg-warning/15 text-warning",
        danger: "bg-destructive/10 text-destructive dark:bg-destructive/20",
        info: "bg-link/15 text-link",
        accent: "bg-link/15 text-link"
      }
    },
    defaultVariants: {
      variant: "default"
    }
  }
)

export type BadgeVariant = NonNullable<VariantProps<typeof badgeVariants>["variant"]>

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span data-slot="badge" className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { badgeVariants }
