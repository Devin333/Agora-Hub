import * as React from "react"
import { Search } from "lucide-react"
import { cn } from "@/lib/utils"

type SearchFieldProps = Omit<React.ComponentProps<"input">, "type"> & {
  /** Classes for the wrapper; use it for width (`max-w-[260px]`, `flex-1`). */
  className?: string
  inputClassName?: string
}

/** Rakazo page filter search box: leading icon, hairline border, focus ring. */
export const SearchField = React.forwardRef<HTMLInputElement, SearchFieldProps>(function SearchField(
  { className, inputClassName, ...props },
  ref
) {
  return (
    <div className={cn("relative w-full", className)}>
      <Search
        size={14}
        strokeWidth={1.9}
        aria-hidden="true"
        className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"
      />
      <input
        ref={ref}
        type="search"
        className={cn(
          "h-8 w-full rounded-lg border border-border bg-background py-1.5 pl-8 pr-3 text-[13px] text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50",
          inputClassName
        )}
        {...props}
      />
    </div>
  )
})
