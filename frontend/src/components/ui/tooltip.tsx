"use client"

import * as TooltipPrimitive from "@radix-ui/react-tooltip"
import { cn } from "@/lib/utils"

export const TooltipProvider = TooltipPrimitive.Provider
export const Tooltip = TooltipPrimitive.Root
export const TooltipTrigger = TooltipPrimitive.Trigger

/** Rakazo tooltips are inverted ink chips with a small arrow. */
export function TooltipContent({
  className,
  sideOffset = 4,
  children,
  showArrow = true,
  ...props
}: React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content> & { showArrow?: boolean }) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        data-slot="tooltip-content"
        sideOffset={sideOffset}
        className={cn(
          "z-50 inline-flex w-fit max-w-xs items-center gap-1.5 rounded-md bg-foreground px-3 py-1.5 text-xs text-background data-[state=closed]:animate-pop-out data-[state=delayed-open]:animate-pop-in data-[state=instant-open]:animate-pop-in",
          className
        )}
        {...props}
      >
        {children}
        {showArrow ? <TooltipPrimitive.Arrow className="fill-foreground" width={10} height={5} /> : null}
      </TooltipPrimitive.Content>
    </TooltipPrimitive.Portal>
  )
}
