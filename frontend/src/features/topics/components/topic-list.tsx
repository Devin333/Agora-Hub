import { EmptyState } from "@/components/common/empty-state";
import type { Topic } from "@/types/topic";
import { TopicCard } from "./topic-card";

const layoutClass: Record<"grid" | "list" | "dense", string> = {
  grid: "grid gap-3 md:grid-cols-2 xl:grid-cols-3",
  list: "space-y-3",
  dense: "grid gap-3 md:grid-cols-2",
};

export function TopicList({ topics, viewMode }: { topics: Topic[]; viewMode: "grid" | "list" | "dense" }) {
  if (!topics.length) {
    return <EmptyState title="没有匹配的主题" description="尝试放宽关键词、趋势、分类或实体筛选。" />;
  }

  return (
    <div className={layoutClass[viewMode]}>
      {topics.map((topic) => (
        <TopicCard key={topic.id} topic={topic} dense={viewMode === "dense"} />
      ))}
    </div>
  );
}
