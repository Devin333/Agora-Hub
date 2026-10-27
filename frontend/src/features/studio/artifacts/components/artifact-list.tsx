"use client";

import { EmptyState } from "@/components/common/empty-state";
import { ArtifactCard } from "@/features/studio/artifacts/components/artifact-card";
import type { Artifact } from "@/types/artifact";

export function ArtifactList({ artifacts, selectedArtifactId, onSelectArtifact }: { artifacts: Artifact[]; selectedArtifactId?: string; onSelectArtifact: (artifactId: string) => void }) {
  if (!artifacts.length) {
    return <EmptyState title="未找到产物" description="调整产物类型、运行 ID 或搜索关键词。" />;
  }

  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {artifacts.map((artifact) => (
        <ArtifactCard key={artifact.id} artifact={artifact} selected={artifact.id === selectedArtifactId} onSelect={() => onSelectArtifact(artifact.id)} />
      ))}
    </div>
  );
}
