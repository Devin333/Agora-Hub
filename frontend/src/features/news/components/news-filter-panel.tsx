import { NativeSelect, NativeSelectOption } from "@/components/common/native-select"
import { Button } from "@/components/ui/button"
import type { NewsFilterOptions, NewsFilters } from "@/types/news"

const captionClass = "mb-1.5 block text-[11px] font-semibold uppercase tracking-wider text-faint-foreground"

export function NewsFilterPanel({
  filters,
  options,
  onChange,
}: {
  filters: NewsFilters
  options: NewsFilterOptions
  onChange: (patch: Partial<NewsFilters>) => void
}) {
  return (
    <aside aria-label="News filters" className="rounded-2xl border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-[15px] font-semibold text-foreground">Filters</h2>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-[12px]"
          onClick={() =>
            onChange({
              dateRange: undefined,
              category: undefined,
              sourceType: undefined,
              topic: undefined,
              credibility: undefined,
              qualityStatus: undefined,
              topicStatus: "all",
              reportStatus: "all",
            })
          }
        >
          Reset
        </Button>
      </div>

      <div className="mt-3 grid gap-5 sm:grid-cols-2">
        <SelectBlock
          label="Period"
          value={filters.dateRange ?? ""}
          onChange={(value) => onChange({ dateRange: value ? (value as NewsFilters["dateRange"]) : undefined })}
          options={[
            ["", "All time"],
            ["today", "Today"],
            ["week", "This week"],
            ["month", "This month"],
          ]}
        />

        <TextBlock
          label="Topic"
          value={filters.topic ?? ""}
          placeholder="agents, models, safety..."
          onChange={(topic) => onChange({ topic: topic || undefined })}
        />

        <CheckboxBlock
          label="Category"
          values={options.categories}
          selected={filters.category ?? []}
          onToggle={(category) => onChange({ category: toggle(filters.category, category) })}
        />

        <CheckboxBlock
          label="Source"
          values={options.sourceTypes}
          selected={filters.sourceType ?? []}
          onToggle={(sourceType) => onChange({ sourceType: toggle(filters.sourceType, sourceType) })}
          formatValue={sourceTypeLabelValue}
        />

        <CheckboxBlock
          label="Credibility"
          values={options.credibility}
          selected={filters.credibility ?? []}
          onToggle={(credibility) => onChange({ credibility: toggle(filters.credibility, credibility) })}
          formatValue={titleCase}
        />

        <CheckboxBlock
          label="Quality"
          values={options.qualityStatuses}
          selected={filters.qualityStatus ?? []}
          onToggle={(qualityStatus) => onChange({ qualityStatus: toggle(filters.qualityStatus, qualityStatus) })}
          formatValue={titleCase}
        />
      </div>
    </aside>
  )
}

function CheckboxBlock<T extends string>({
  label,
  values,
  selected,
  onToggle,
  formatValue = titleCase,
}: {
  label: string
  values: T[]
  selected: T[]
  onToggle: (value: T) => void
  formatValue?: (value: T) => string
}) {
  return (
    <fieldset>
      <legend className={captionClass}>{label}</legend>
      <div className="space-y-1">
        {values.map((value) => (
          <label key={value} className="flex cursor-pointer items-center gap-2 rounded-lg px-1 py-1 text-body2 text-foreground/85 hover:bg-accent/40">
            <input
              type="checkbox"
              checked={selected.includes(value)}
              onChange={() => onToggle(value)}
              className="size-4 rounded border-input accent-primary focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
            />
            <span>{formatValue(value)}</span>
          </label>
        ))}
      </div>
    </fieldset>
  )
}

function SelectBlock({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: Array<[string, string]>
  onChange: (value: string) => void
}) {
  return (
    <label className="block">
      <span className={captionClass}>{label}</span>
      <NativeSelect className="w-full" value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map(([optionValue, labelText]) => (
          <NativeSelectOption key={optionValue} value={optionValue}>
            {labelText}
          </NativeSelectOption>
        ))}
      </NativeSelect>
    </label>
  )
}

function TextBlock({
  label,
  value,
  placeholder,
  onChange,
}: {
  label: string
  value: string
  placeholder: string
  onChange: (value: string) => void
}) {
  return (
    <label className="block">
      <span className={captionClass}>{label}</span>
      <input
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-[13px] text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
      />
    </label>
  )
}

function toggle<T extends string>(values: T[] | undefined, value: T) {
  const set = new Set(values ?? [])
  if (set.has(value)) {
    set.delete(value)
  } else {
    set.add(value)
  }
  return [...set]
}

function sourceTypeLabelValue(value: string) {
  const labels: Record<string, string> = {
    official_blog: "Official blog",
    rss: "RSS",
    atom: "Atom",
    github: "GitHub",
    hackernews: "Hacker News",
    reddit: "Reddit",
    arxiv: "arXiv",
    media: "Media",
    custom: "Custom",
  }
  return labels[value] ?? titleCase(value)
}

function titleCase(value: string) {
  return value.replace(/[_-]/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase())
}
