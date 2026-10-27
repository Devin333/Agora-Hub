"use client"

import type { ReactNode } from "react"
import { NativeSelect, NativeSelectOption } from "@/components/common/native-select"
import { SearchField } from "@/components/common/search-field"
import { cn } from "@/lib/utils"

/**
 * Rakazo page filter controls shared by the Studio list pages. Each control keeps a
 * visible caption label so the native form semantics stay intact.
 */

export function StudioFilterRow({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("flex flex-wrap items-end gap-2", className)}>{children}</div>
}

export function StudioSearchFilter({
  value,
  placeholder,
  onChange,
  className
}: {
  value: string
  placeholder: string
  onChange: (value: string) => void
  className?: string
}) {
  return (
    <SearchField
      className={cn("w-full sm:max-w-[260px]", className)}
      aria-label={placeholder}
      placeholder={placeholder}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  )
}

export function StudioSelectFilter({
  label,
  value,
  options,
  allLabel,
  optionLabel,
  onChange,
  includeAll = true,
  className
}: {
  label: string
  value: string
  options: string[]
  allLabel?: string
  optionLabel?: (value: string) => string
  onChange: (value: string) => void
  /** Prepend an empty "all" option that clears the filter. */
  includeAll?: boolean
  className?: string
}) {
  return (
    <label className={`grid min-w-[8.5rem] gap-1 text-label text-muted-foreground ${className ?? ""}`}>
      {label}
      <NativeSelect className="w-full" value={value} onChange={(event) => onChange(event.target.value)}>
        {includeAll ? <NativeSelectOption value="">{allLabel ?? "All"}</NativeSelectOption> : null}
        {options.map((option) => (
          <NativeSelectOption key={option} value={option}>
            {optionLabel?.(option) ?? option}
          </NativeSelectOption>
        ))}
      </NativeSelect>
    </label>
  )
}

export function StudioCheckboxFilter({ label, checked, onChange }: { label: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <label className="inline-flex h-8 cursor-pointer items-center gap-2 rounded-lg border border-border px-3 text-[13px] text-muted-foreground transition-colors hover:bg-accent/40 has-[:checked]:text-foreground has-[:focus-visible]:ring-3 has-[:focus-visible]:ring-ring/50">
      <input type="checkbox" className="size-3.5 accent-primary outline-none" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      {label}
    </label>
  )
}

export function StudioNumberFilter({
  label,
  value,
  min,
  max,
  onChange
}: {
  label: string
  value?: number
  min?: number
  max?: number
  onChange: (value: number | undefined) => void
}) {
  return (
    <label className="inline-flex h-8 items-center gap-2 rounded-lg border border-border pe-1 ps-3 text-[13px] text-muted-foreground has-[:focus-visible]:ring-3 has-[:focus-visible]:ring-ring/50">
      {label}
      <input
        className="h-6 w-16 rounded-md bg-muted px-2 text-foreground tabular-nums outline-none"
        type="number"
        min={min}
        max={max}
        value={value ?? ""}
        onChange={(event) => onChange(event.target.value ? Number(event.target.value) : undefined)}
      />
    </label>
  )
}

export type StudioSegmentTab<T extends string> = { id: T; label: string }

/**
 * Segmented view switch (rakazo Artifacts grid/list toggle) used for inspector tabs.
 * Buttons expose `aria-pressed` so the active view is announced.
 */
export function StudioSegmentTabs<T extends string>({
  tabs,
  active,
  onChange,
  label,
  className
}: {
  tabs: Array<StudioSegmentTab<T>>
  active: T
  onChange: (id: T) => void
  label?: string
  className?: string
}) {
  return (
    <div role="group" aria-label={label} className={cn("rk-scroll flex gap-0.5 overflow-x-auto rounded-lg border border-border p-0.5", className)}>
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          aria-pressed={active === tab.id}
          className="h-7 shrink-0 rounded-md px-2.5 text-[13px] font-medium text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 aria-pressed:bg-accent aria-pressed:text-accent-foreground"
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  )
}
