import { SectionPanel } from "@/components/common/section-panel";
import type { AgentAnalysis } from "@/types/topic";

export function AgentAnalysisPanel({ analyses }: { analyses: AgentAnalysis[] }) {
  return (
    <SectionPanel title="智能体分析" framed={false}>
      {analyses.length ? (
        <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border">
          {analyses.map((analysis) => (
            <li key={analysis.agent} className="px-4 py-3">
              <p className="text-body2 font-medium text-foreground">{analysis.agent}</p>
              <p className="mt-0.5 text-[13px] leading-relaxed text-muted-foreground">{analysis.summary}</p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="py-6 text-center text-body2 text-muted-foreground">暂无智能体分析。</p>
      )}
    </SectionPanel>
  );
}
