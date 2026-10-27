"use client";

import { NativeSelect, NativeSelectOption } from "@/components/common/native-select";
import { SearchField } from "@/components/common/search-field";
import type { TechFilters } from "@/features/tech/hooks/use-tech-items";
import type { TechItemType, TechMaturity } from "@/types/tech";

const types: TechItemType[] = ["paper", "repo", "framework", "method", "practice"];
const maturities: TechMaturity[] = ["experimental", "emerging", "stable", "mature"];
const typeLabels: Record<TechItemType, string> = {
  paper: "论文",
  repo: "仓库",
  framework: "框架",
  method: "方法",
  practice: "实践",
};
const maturityLabels: Record<TechMaturity, string> = {
  experimental: "实验",
  emerging: "新兴",
  stable: "稳定",
  mature: "成熟",
};

/** Rakazo page filter row: search box on the left, compact selects on the right. */
export function TechFilterToolbar({ filters, onChange }: { filters: TechFilters; onChange: (filters: TechFilters) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <SearchField
        className="min-w-[14rem] flex-1 md:max-w-[360px]"
        placeholder="搜索技术、主题、标签..."
        aria-label="搜索技术、主题、标签"
        value={filters.keyword ?? ""}
        onChange={(event) => onChange({ ...filters, keyword: event.target.value })}
      />
      <div className="flex flex-wrap items-center gap-2 md:ml-auto">
        <NativeSelect
          aria-label="技术类型"
          value={filters.type ?? ""}
          onChange={(event) => onChange({ ...filters, type: (event.target.value || undefined) as TechItemType | undefined })}
        >
          <NativeSelectOption value="">全部类型</NativeSelectOption>
          {types.map((type) => (
            <NativeSelectOption key={type} value={type}>
              {typeLabels[type]}
            </NativeSelectOption>
          ))}
        </NativeSelect>
        <NativeSelect
          aria-label="成熟度"
          value={filters.maturity ?? ""}
          onChange={(event) => onChange({ ...filters, maturity: (event.target.value || undefined) as TechMaturity | undefined })}
        >
          <NativeSelectOption value="">全部成熟度</NativeSelectOption>
          {maturities.map((maturity) => (
            <NativeSelectOption key={maturity} value={maturity}>
              {maturityLabels[maturity]}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </div>
    </div>
  );
}
