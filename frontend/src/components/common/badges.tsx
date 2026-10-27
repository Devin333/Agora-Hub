import { ExternalLink } from "lucide-react";
import Link from "next/link";
import { Badge as UiBadge, type BadgeVariant } from "@/components/ui/badge";
import { formatScore, titleCase } from "@/lib/format";
import type { CredibilityLevel, SourceType } from "@/types/source";
import type { TechMaturity, TechItemType } from "@/types/tech";
import type { TopicTrend } from "@/types/topic";

const toneVariant = {
  good: "success",
  info: "info",
  warn: "warning",
  bad: "danger",
  neutral: "default",
  accent: "accent",
} satisfies Record<string, BadgeVariant>;

export function Badge({ children, tone = "neutral" }: { children: React.ReactNode; tone?: keyof typeof toneVariant }) {
  return <UiBadge variant={toneVariant[tone]}>{children}</UiBadge>;
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
  const external = href.startsWith("http");
  return (
    <Link
      className="inline-flex items-center gap-1 rounded-sm text-body2 font-medium text-link underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
      href={href}
      target={external ? "_blank" : undefined}
      rel={external ? "noreferrer" : undefined}
    >
      {children}
      {external ? <ExternalLink size={14} strokeWidth={1.8} aria-hidden="true" /> : null}
    </Link>
  );
}
