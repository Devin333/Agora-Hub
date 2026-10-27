import { clsx, type ClassValue } from "clsx"
import { extendTailwindMerge } from "tailwind-merge"
import { typeScaleNames } from "@/styles/type-scale"

/** tailwind-merge that knows the custom type scale, so `text-label` and `text-muted-foreground` both survive. */
const twMerge = extendTailwindMerge({
  extend: { classGroups: { "font-size": [{ text: typeScaleNames }] } }
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
