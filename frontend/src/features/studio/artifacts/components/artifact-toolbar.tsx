"use client";

import { NativeSelect, NativeSelectOption } from "@/components/common/native-select";
import { SearchField } from "@/components/common/search-field";
import { useI18n } from "@/lib/i18n/use-i18n";
import type { ArtifactFilters } from "@/types/artifact";

const artifactTypes: Array<Exclude<ArtifactFilters["artifactType"], "all">> = ["json", "markdown", "html", "log", "report", "dataset"];
const artifactLabels: Record<Exclude<ArtifactFilters["artifactType"], "all">, { zh: string; en: string }> = {
  json: { zh: "JSON", en: "JSON" },
  markdown: { zh: "Markdown", en: "Markdown" },
  html: { zh: "HTML", en: "HTML" },
  log: { zh: "日志", en: "Log" },
  report: { zh: "报告", en: "Report" },
  dataset: { zh: "数据集", en: "Dataset" },
};

/** Rakazo page filter row: search, type select and run id field side by side. */
export function ArtifactToolbar({ filters, onChange }: { filters: ArtifactFilters; onChange: (filters: ArtifactFilters) => void }) {
  const { locale, t } = useI18n();
  return (
    <section className="flex flex-wrap items-center gap-2">
      <SearchField
        className="sm:max-w-[260px]"
        aria-label={t("studio.artifacts.searchArtifacts")}
        placeholder={t("studio.artifacts.searchArtifacts")}
        value={filters.keyword}
        onChange={(event) => onChange({ ...filters, keyword: event.target.value })}
      />
      <NativeSelect
        className="w-auto"
        aria-label={t("studio.artifacts.allTypes")}
        value={filters.artifactType}
        onChange={(event) => onChange({ ...filters, artifactType: event.target.value as ArtifactFilters["artifactType"] })}
      >
        <NativeSelectOption value="all">{t("studio.artifacts.allTypes")}</NativeSelectOption>
        {artifactTypes.map((type) => (
          <NativeSelectOption key={type} value={type}>
            {artifactLabels[type][locale]}
          </NativeSelectOption>
        ))}
      </NativeSelect>
      <input
        className="h-8 w-full rounded-lg border border-border bg-background px-3 text-[13px] text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 sm:w-48"
        aria-label={t("studio.review.runId")}
        placeholder={t("studio.review.runId")}
        value={filters.runId}
        onChange={(event) => onChange({ ...filters, runId: event.target.value })}
      />
    </section>
  );
}
