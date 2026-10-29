import type { TopicFilters } from "@/types/topic";

export function TopicFilterPanel({ filters }: { filters: TopicFilters }) {
  const activeCount = Object.values(filters).filter(Boolean).length;
  return (
    <p className="text-label text-muted-foreground" role="status">
      Active filters: <span className="tabular-nums text-foreground">{activeCount || "none"}</span>
    </p>
  );
}
