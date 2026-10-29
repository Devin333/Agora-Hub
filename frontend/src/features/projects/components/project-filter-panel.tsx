import { Button } from "@/components/ui/button"
import { ProjectNativeSelect, ProjectNativeSelectOption } from "@/features/projects/components/project-page-layout"
import { cn } from "@/lib/utils"
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
    <aside className="space-y-5">
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
      <label className="grid gap-1.5">
        <span className={sectionTitleClassName}>License</span>
        <ProjectNativeSelect
          wrapperClassName="w-full"
          aria-label="License"
          value={filters.license ?? ""}
          onChange={(event) => onChange({ license: (event.target.value || undefined) as ProjectListParams["license"], page: 1, cursor: undefined })}
        >
          <ProjectNativeSelectOption value="">All licenses</ProjectNativeSelectOption>
          <ProjectNativeSelectOption value="MIT">MIT</ProjectNativeSelectOption>
          <ProjectNativeSelectOption value="Apache-2.0">Apache-2.0</ProjectNativeSelectOption>
          <ProjectNativeSelectOption value="BSD-3-Clause">BSD-3-Clause</ProjectNativeSelectOption>
        </ProjectNativeSelect>
      </label>
    </aside>
  )
}

const sectionTitleClassName = "px-2.5 text-[11px] font-semibold uppercase tracking-wider text-faint-foreground"

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
    <section className="space-y-1">
      <div className="flex min-h-7 items-center justify-between gap-2">
        <h2 className={sectionTitleClassName}>{title}</h2>
        {activeValue ? (
          <Button type="button" variant="ghost" size="xs" onClick={onClear}>
            Clear
          </Button>
        ) : null}
      </div>
      {options.length ? (
        <div className="space-y-0.5">
          {options.slice(0, 10).map((option) => {
            const active = activeValue === option.value
            return (
              <button
                key={option.value}
                type="button"
                aria-pressed={active}
                onClick={() => onSelect(option.value)}
                className={cn(
                  "flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-left text-body2 outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50",
                  active ? "bg-accent font-medium text-accent-foreground" : "text-muted-foreground hover:bg-accent/60 hover:text-foreground"
                )}
              >
                <span className="truncate">{option.label}</span>
                <span className="shrink-0 text-label text-faint-foreground">{option.count}</span>
              </button>
            )
          })}
        </div>
      ) : (
        <p className="px-2.5 py-1 text-label text-muted-foreground">No available options from current project data.</p>
      )}
    </section>
  )
}
