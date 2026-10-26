import * as React from "react"
import { cn } from "@/lib/utils"

export type InputProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, "size"> & {
  /** `xl` is the tall auth-form field. */
  size?: "default" | "xl"
}

export const inputClassName =
  "flex h-9 w-full min-w-0 rounded-lg border border-input bg-muted px-3 py-1 text-sm text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 aria-[invalid=true]:border-destructive aria-[invalid=true]:ring-3 aria-[invalid=true]:ring-destructive/20 file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground data-[size=xl]:h-12 data-[size=xl]:rounded-xl data-[size=xl]:px-4 data-[size=xl]:text-base"

export const Input = React.forwardRef<HTMLInputElement, InputProps>(({ className, type, size = "default", ...props }, ref) => (
  <input ref={ref} type={type} data-slot="input" data-size={size} className={cn(inputClassName, className)} {...props} />
))
Input.displayName = "Input"
