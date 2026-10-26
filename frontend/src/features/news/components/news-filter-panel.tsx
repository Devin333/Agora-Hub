import type { NewsFilterOptions, NewsFilters } from "@/types/news"

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
    <aside className="rounded-md border border-[#dbe3dc] bg-white/85 p-4 dark:border-border dark:bg-card">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-[#334155] dark:text-foreground">Filters</h2>
        <button
          type="button"
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
          className="text-xs font-medium text-emerald-700 hover:text-[#334155] dark:text-link dark:hover:text-foreground"
        >
          Reset
        </button>
      </div>

      <div className="mt-4 space-y-5">
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
    <div>
      <p className="mb-2 text-xs font-medium uppercase text-[#334155]/55 dark:text-muted-foreground">{label}</p>
      <div className="space-y-2">
        {values.map((value) => (
          <label key={value} className="flex items-center gap-2 text-sm text-[#334155]/68 dark:text-muted-foreground">
            <input
              type="checkbox"
              checked={selected.includes(value)}
              onChange={() => onToggle(value)}
              className="h-4 w-4 rounded border-[#dbe3dc] bg-white accent-emerald-700 dark:border-border dark:bg-background"
            />
            <span>{formatValue(value)}</span>
          </label>
        ))}
      </div>
    </div>
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
      <span className="mb-2 block text-xs font-medium uppercase text-[#334155]/55 dark:text-muted-foreground">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-10 w-full rounded-md border border-[#dbe3dc] bg-white px-3 text-sm text-[#334155] outline-none dark:border-border dark:bg-background dark:text-foreground"
      >
        {options.map(([optionValue, labelText]) => (
          <option key={optionValue} value={optionValue}>
            {labelText}
          </option>
        ))}
      </select>
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
      <span className="mb-2 block text-xs font-medium uppercase text-[#334155]/55 dark:text-muted-foreground">{label}</span>
      <input
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="h-10 w-full rounded-md border border-[#dbe3dc] bg-white px-3 text-sm text-[#334155] outline-none placeholder:text-[#334155]/40 dark:border-border dark:bg-background dark:text-foreground"
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
