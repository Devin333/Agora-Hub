"use client"

import { FormEvent, useEffect, useState } from "react"
import { Filter, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { ProjectSearchInput } from "@/features/projects/components/project-page-layout"
import type { ProjectListParams } from "@/types/projects"

export function ProjectToolbar({
  filters,
  onChange,
  onToggleFilters,
}: {
  filters: ProjectListParams
  onChange: (patch: Partial<ProjectListParams>) => void
  onToggleFilters: () => void
}) {
  const [query, setQuery] = useState(filters.q ?? "")

  useEffect(() => {
    setQuery(filters.q ?? "")
  }, [filters.q])

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    onChange({ q: query.trim() || undefined, page: 1, cursor: undefined })
  }

  function resetFilters() {
    onChange({
      q: undefined,
      category: undefined,
      topic: undefined,
      source: undefined,
      language: undefined,
      license: undefined,
      maturity: undefined,
      period: undefined,
      page: 1,
      cursor: undefined,
    })
  }

  return (
    <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
      <form className="flex min-w-0 flex-1 gap-2" onSubmit={submitSearch}>
        <ProjectSearchInput
          className="flex-1"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search repositories, owners, tags, or engineering problems"
          aria-label="Search projects"
        />
        {query ? (
          <Button
            type="button"
            variant="ghost"
            size="icon-lg"
            aria-label="Clear search"
            onClick={() => {
              setQuery("")
              onChange({ q: undefined, page: 1, cursor: undefined })
            }}
          >
            <X strokeWidth={1.8} />
          </Button>
        ) : null}
        <Button type="submit" size="lg">Search</Button>
      </form>
      <div className="flex items-center gap-2">
        <Button type="button" variant="outline" size="lg" className="xl:hidden" onClick={onToggleFilters}>
          <Filter strokeWidth={1.8} />
          Filters
        </Button>
        <Button type="button" variant="ghost" size="lg" onClick={resetFilters}>
          Reset
        </Button>
      </div>
    </div>
  )
}
