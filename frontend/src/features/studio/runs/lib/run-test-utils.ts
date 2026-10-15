import type { AgentRun, AgentRunFilters } from "@/types/agent";

export function filterRunsForTest(runs: AgentRun[], filters: AgentRunFilters): AgentRun[] {
  const keyword = filters.keyword?.trim().toLowerCase();
  const now = Date.parse("2026-05-22T09:00:00.000Z");

  return runs
    .filter((run) => {
      if (!keyword) return true;
      return [run.id, run.agentName, run.profile, run.workflowName, run.status].filter(Boolean).join(" ").toLowerCase().includes(keyword);
    })
    .filter((run) => (!filters.agentName?.length ? true : filters.agentName.includes(run.agentName)))
    .filter((run) => (!filters.status?.length ? true : filters.status.includes(run.status)))
    .filter((run) => (!filters.profile?.length ? true : filters.profile.includes(run.profile)))
    .filter((run) => (!filters.hasError ? true : run.errorCount > 0))
    .filter((run) => (filters.minQualityScore === undefined ? true : (run.qualityScore ?? 0) >= filters.minQualityScore))
    .filter((run) => {
      if (!filters.dateRange || filters.dateRange === "custom") return true;
      const startedAt = new Date(run.startedAt).getTime();
      const windows = {
        today: 24 * 60 * 60 * 1000,
        week: 7 * 24 * 60 * 60 * 1000,
        month: 30 * 24 * 60 * 60 * 1000,
      };
      return now - startedAt <= windows[filters.dateRange];
    })
    .sort((a, b) => {
      const sort = filters.sort ?? "startedAt";
      if (sort === "durationMs") return (b.durationMs ?? 0) - (a.durationMs ?? 0);
      if (sort === "qualityScore") return (b.qualityScore ?? 0) - (a.qualityScore ?? 0);
      if (sort === "errorCount") return b.errorCount - a.errorCount;
      return new Date(b.startedAt).getTime() - new Date(a.startedAt).getTime();
    });
}
