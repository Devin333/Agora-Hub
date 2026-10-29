import { EmptyState } from "@/components/common/empty-state";
import type { TechItem } from "@/types/tech";
import { TechItemCard } from "./tech-item-card";

export function TechRadarGrid({ items }: { items: TechItem[] }) {
  if (!items.length) {
    return <EmptyState title="未找到技术项" description="可以放宽类型或成熟度筛选。" />;
  }

  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {items.map((item) => (
        <TechItemCard key={item.id} item={item} />
      ))}
    </div>
  );
}
