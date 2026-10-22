"""整体系统测评：端到端(检索→生成) → Answer Correctness + 检索命中 + 端到端成功率。

与生成测评的区别：这里是端到端的，检索可能失败，评估最终答案是否正确
(以问题来源的 source chunk 内容作为 ground truth)。

用法:
  python data/eval/run_system_eval.py            # 默认抽样 18 题 (每领域 3)
  python data/eval/run_system_eval.py --full     # 全部黄金集
"""
from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from framework.shared.env import load_root_env

load_root_env()

from infrastructure.storage.vector.qdrant_store import qdrant_store_from_env
from infrastructure.storage.vector.paper_chunk_store import PaperChunkStore
from infrastructure.external.reranker import CrossEncoderReranker
from backend.research.document.chunk_storage import PaperChunkStoreAdapter
from backend.research.rag.retrieval.paper_retriever import ResearchRetriever
from backend.research.rag.retrieval.contracts import RetrievalRequest
from backend.research.rag.retrieval.paper_answer_generator import AnswerGenerator
from backend.research.rag.evaluation.paper_system_eval import SystemEvaluator, SystemSample
from backend.research.rag.evaluation.paper_gold_builder import load_golden_set
from backend.research.application.llm_client import build_unity_judge_llm_call, build_unity_paper_answer_model

GOLDEN = Path("data/eval/golden_set.json")
PER_DOMAIN = 3


def _sample(pairs, full: bool):
    if full:
        return pairs
    by_domain = defaultdict(list)
    for qa in pairs:
        by_domain[qa.domain].append(qa)
    out = []
    for items in by_domain.values():
        out.extend(items[:PER_DOMAIN])
    return out


async def main() -> None:
    full = "--full" in sys.argv
    pairs = _sample(load_golden_set(GOLDEN), full)
    print(f"整体系统测评样本: {len(pairs)} 题\n")

    store = PaperChunkStoreAdapter(PaperChunkStore(qdrant_store_from_env()))
    retriever = ResearchRetriever(store, reranker=CrossEncoderReranker())
    generator = AnswerGenerator(build_unity_paper_answer_model(max_tokens=600))
    evaluator = SystemEvaluator(build_unity_judge_llm_call(max_tokens=200), max_concurrency=3)

    samples: list[SystemSample] = []
    for i, qa in enumerate(pairs, 1):
        # ground truth = content of the source chunk the question was derived from
        source = store.get_chunk(qa.source_chunk_id)
        ground_truth = source.content if source else ""

        # end-to-end: retrieve (system doesn't know the source) → generate
        retrieval = retriever.retrieve(RetrievalRequest(paper_id=qa.paper_id, question=qa.question, limit=6))
        hit = any(c.chunk_id == qa.source_chunk_id for c in retrieval.child_chunks)
        ans = await generator.generate(qa.question, retrieval)

        samples.append(SystemSample(
            question=qa.question,
            generated_answer=ans.answer,
            ground_truth=ground_truth,
            retrieval_hit=hit,
        ))
        print(f"[{i}/{len(pairs)}] hit={'Y' if hit else 'N'}  {qa.question[:55]}")

    print("\n评测中...")
    result = await evaluator.evaluate(samples)
    print("\n" + result.report())


if __name__ == "__main__":
    asyncio.run(main())
