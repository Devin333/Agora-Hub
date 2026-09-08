import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import type { ProjectFilterOption, ProjectListOptions, ProjectListParams } from "@/types/projects"

export function ProjectFilterPanel({
  filters,
  options,
  onChange,
}: {
  filters: ProjectListParams
  options: ProjectListOptions
  onChange: (patch: Partial<ProjectListParams>) => void
}) {
  return (
    <aside className="space-y-7">
      <FilterGroup
        title="Topics"
        activeValue={filters.topic}
        options={options.topics}
        onSelect={(topic) => onChange({ topic, page: 1, cursor: undefined })}
        onClear={() => onChange({ topic: undefined, page: 1, cursor: undefined })}
      />
      <FilterGroup
        title="Categories"
        activeValue={filters.category}
        options={options.categories}
        onSelect={(category) => onChange({ category: category as ProjectListParams["category"], page: 1, cursor: undefined })}
        onClear={() => onChange({ category: undefined, page: 1, cursor: undefined })}
      />
      <FilterGroup
        title="Languages"
        activeValue={filters.language}
        options={options.languages}
        onSelect={(language) => onChange({ language: language as ProjectListParams["language"], page: 1, cursor: undefined })}
        onClear={() => onChange({ language: undefined, page: 1, cursor: undefined })}
      />
      <FilterGroup
        title="Maturity"
        activeValue={filters.maturity}
        options={options.maturity}
        onSelect={(maturity) => onChange({ maturity: maturity as ProjectListParams["maturity"], page: 1, cursor: undefined })}
        onClear={() => onChange({ maturity: undefined, page: 1, cursor: undefined })}
      />
      <FilterGroup
        title="Sources"
        activeValue={filters.source}
        options={options.sources}
        onSelect={(source) => onChange({ source: source as ProjectListParams["source"], page: 1, cursor: undefined })}
        onClear={() => onChange({ source: undefined, page: 1, cursor: undefined })}
      />
      <label className="grid gap-2 text-xs font-semibold uppercase tracking-[0.12em] text-[#334155]/55 dark:text-muted-foreground">
        License
        <select className="rounded-md border border-[#dbe3dc] bg-white px-3 py-2 text-sm font-normal normal-case tracking-normal text-[#334155] dark:border-border dark:bg-card dark:text-foreground" aria-label="License" value={filters.license ?? ""} onChange={(event) => onChange({ license: (event.target.value || undefined) as ProjectListParams["license"], page: 1, cursor: undefined })}>
          <option value="">All licenses</option><option value="MIT">MIT</option><option value="Apache-2.0">Apache-2.0</option><option value="BSD-3-Clause">BSD-3-Clause</option>
        </select>
      </label>
    </aside>
  )
}

function FilterGroup({
  title,
  activeValue,
  options,
  onSelect,
  onClear,
}: {
  title: string
  activeValue?: string
  options: ProjectFilterOption[]
  onSelect: (value: string) => void
  onClear: () => void
}) {
  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="font-mono text-[11px] font-semibold uppercase tracking-normal text-[#334155]/55 dark:text-muted-foreground">{title}</h2>
        {activeValue ? (
          <Button type="button" variant="ghost" size="sm" className="h-7 px-2" onClick={onClear}>
            Clear
          </Button>
        ) : null}
      </div>
      {options.length ? (
        <div className="space-y-2">
          {options.slice(0, 10).map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => onSelect(option.value)}
              className={
                activeValue === option.value
                  ? "flex w-full items-center justify-between gap-2 rounded-md bg-[#334155] px-3 py-2 text-left text-sm text-white"
                  : "flex w-full items-center justify-between gap-2 rounded-md px-3 py-2 text-left text-sm text-[#334155]/68 hover:bg-white hover:text-[#334155] dark:text-muted-foreground dark:hover:bg-card dark:hover:text-foreground"
              }
            >
              <span className="truncate">{option.label}</span>
              <Badge variant={activeValue === option.value ? "muted" : "default"}>{option.count}</Badge>
            </button>
          ))}
        </div>
      ) : (
        <p className="rounded-md border border-dashed border-[#dbe3dc] px-3 py-4 text-xs leading-5 text-[#334155]/55 dark:border-border dark:text-muted-foreground">
          No available options from current project data.
        </p>
      )}
    </section>
  )
}
