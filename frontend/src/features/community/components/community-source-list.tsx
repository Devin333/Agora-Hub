import { cn } from "@/lib/utils"
import { communitySourceLabel, communityTopicLabel } from "@/lib/community/community-filters"
import type { CommunityFilterOptions, CommunityListParams, CommunitySourceType, CommunityTopicKey } from "@/types/community"

export function CommunitySourceList({
  options,
  filters,
  onChange
}: {
  options: CommunityFilterOptions
  filters: CommunityListParams
  onChange: (patch: Partial<CommunityListParams>) => void
}) {
  return (
    <aside aria-label="Community sources" className="hidden space-y-5 xl:block">
      <SourceBlock
        title="Sources"
        items={options.sources}
        active={filters.source}
        label={(value) => communitySourceLabel(value)}
        onSelect={(source) => onChange({ source: filters.source === source ? undefined : source })}
      />
      <SourceBlock
        title="Topics"
        items={options.topics}
        active={filters.topic}
        label={(value) => communityTopicLabel(value)}
        onSelect={(topic) => onChange({ topic: filters.topic === topic ? undefined : topic })}
      />
    </aside>
  )
}

function SourceBlock<T extends CommunitySourceType | CommunityTopicKey>({
  title,
  items,
  active,
  label,
  onSelect
}: {
  title: string
  items: Array<{ count: number } & Record<string, T | number | string>>
  active?: T
  label: (value: T) => string
  onSelect: (value: T) => void
}) {
  return (
    <section>
      <h2 className="px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">{title}</h2>
      <div className="mt-0.5 space-y-0.5">
        {items.map((item) => {
          const value = ("sourceType" in item ? item.sourceType : item.topic) as T
          return (
            <button
              key={value}
              type="button"
              aria-pressed={active === value}
              onClick={() => onSelect(value)}
              className={`${cn(
                "flex w-full items-baseline justify-between gap-3 rounded-lg px-2.5 py-1.5 text-left outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50",
                active === value
                  ? "bg-accent font-medium text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent/60 hover:text-foreground"
              )} text-body2`}
            >
              <span className="truncate">{label(value)}</span>
              <span className="text-label tabular-nums text-faint-foreground">{item.count}</span>
            </button>
          )
        })}
      </div>
    </section>
  )
}
