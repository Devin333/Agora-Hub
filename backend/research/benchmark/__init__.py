from __future__ import annotations

from backend.research.benchmark.gates import (
    validate_benchmark_score_refs,
    validate_score_range,
    validate_sota_claim_status,
)
from backend.research.benchmark.models import (
    ResearchBaseline,
    ResearchBenchmark,
    ResearchDataset,
    ResearchMetric,
    ResearchSOTAClaim,
    ResearchScore,
)

__all__ = [
    "ResearchBaseline",
    "ResearchBenchmark",
    "ResearchDataset",
    "ResearchMetric",
    "ResearchSOTAClaim",
    "ResearchScore",
    "validate_benchmark_score_refs",
    "validate_score_range",
    "validate_sota_claim_status",
]

from backend.research.benchmark.reportbench import (
    CrossSourceResearchResult,
    ReportBenchCitation,
    ReportBenchExecution,
    ReportBenchIssue,
    ReportBenchSource,
    ReportBenchTask,
    validate_result,
    validate_tasks,
)

__all__ += [
    "CrossSourceResearchResult",
    "ReportBenchCitation",
    "ReportBenchExecution",
    "ReportBenchIssue",
    "ReportBenchSource",
    "ReportBenchTask",
    "validate_result",
    "validate_tasks",
]
