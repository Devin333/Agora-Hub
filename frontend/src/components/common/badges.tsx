import { ExternalLink } from "lucide-react";
import Link from "next/link";
import { cn, formatScore, titleCase } from "@/lib/format";
import type { CredibilityLevel, SourceType } from "@/types/source";
import type { TechMaturity, TechItemType } from "@/types/tech";
import type { TopicTrend } from "@/types/topic";

const toneClasses = {
  good: "border-success/30 bg-success/10 text-success",
  info: "border-link/30 bg-link/10 text-link",
  warn: "border-warning/30 bg-warning/10 text-warning",
  bad: "border-destructive/30 bg-destructive/10 text-destructive",
  neutral: "border-border bg-secondary text-muted-foreground",
  accent: "border-primary/30 bg-primary/10 text-primary",
};

export function Badge({ children, tone = "neutral" }: { children: React.ReactNode; tone?: keyof typeof toneClasses }) {
  return (
    <span className={cn("inline-flex min-h-6 items-center rounded-md border px-2 py-1 text-xs font-medium", toneClasses[tone])}>
      {children}
    </span>
  );
}

export function TrendBadge({ trend }: { trend: TopicTrend }) {
  const tone = trend === "rising" ? "good" : trend === "falling" ? "warn" : "info";
  return <Badge tone={tone}>{titleCase(trend)}</Badge>;
}

export function CredibilityBadge({ credibility }: { credibility: CredibilityLevel }) {
  const tone = credibility === "high" ? "good" : credibility === "medium" ? "warn" : "bad";
  return <Badge tone={tone}>{titleCase(credibility)}可信</Badge>;
}

export function SourceBadge({ sourceType, sourceName }: { sourceType: SourceType; sourceName?: string }) {
  return <Badge tone={sourceType === "official_blog" || sourceType === "arxiv" ? "accent" : "neutral"}>{sourceName ?? titleCase(sourceType)}</Badge>;
}

export function QualityBadge({ score }: { score?: number }) {
  const tone = score === undefined ? "neutral" : score >= 85 ? "good" : score >= 70 ? "warn" : "bad";
  return <Badge tone={tone}>质量 {formatScore(score)}</Badge>;
}

export function HeatScoreBadge({ score }: { score?: number }) {
  const tone = score === undefined ? "neutral" : score >= 85 ? "bad" : score >= 70 ? "warn" : "info";
  return <Badge tone={tone}>热度 {formatScore(score)}</Badge>;
}

export function MaturityBadge({ maturity }: { maturity: TechMaturity }) {
  const tone = maturity === "mature" || maturity === "stable" ? "good" : maturity === "emerging" ? "info" : "warn";
  return <Badge tone={tone}>{titleCase(maturity)}</Badge>;
}

export function TechTypeBadge({ type }: { type: TechItemType }) {
  return <Badge tone="accent">{titleCase(type)}</Badge>;
}

export function ExternalTextLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link className="inline-flex items-center gap-1 text-sm font-medium text-primary hover:underline" href={href} target={href.startsWith("http") ? "_blank" : undefined}>
      {children}
      {href.startsWith("http") ? <ExternalLink className="h-3.5 w-3.5" /> : null}
    </Link>
  );
}
