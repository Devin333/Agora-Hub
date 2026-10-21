from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from backend.research.rag.evaluation.golden_set_paper_ingest import (
    DEFAULT_GOLDEN_SET_PATH,
    DEFAULT_MANIFEST_PATH,
    DEFAULT_PAPERS_DIR,
    ingest_golden_set_papers,
)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = ingest_golden_set_papers(
        golden_set=Path(args.golden_set),
        papers_dir=Path(args.papers_dir),
        manifest_path=Path(args.manifest) if args.manifest else None,
        force=args.force,
        max_papers=args.max_papers,
        pdf_parser_backend=args.pdf_parser_backend,
        with_pdf_sidecar=args.with_pdf_sidecar,
        pdf_sidecar_mode=args.pdf_sidecar_mode,
        merge_pdf_visuals=not args.no_merge_pdf_visuals,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), end="\n")
    return 0 if report.failed == 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.research.rag.cli.ingest_golden_set_papers",
        description="Fetch and parse missing real-corpus papers referenced by the RAG golden set.",
    )
    parser.add_argument("--golden-set", default=str(DEFAULT_GOLDEN_SET_PATH))
    parser.add_argument("--papers-dir", default=str(DEFAULT_PAPERS_DIR))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH))
    parser.add_argument("--max-papers", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--pdf-parser-backend",
        choices=("nougat", "marker", "cascade"),
        default=None,
    )
    parser.add_argument("--with-pdf-sidecar", action="store_true")
    parser.add_argument(
        "--pdf-sidecar-mode",
        choices=("missing", "always"),
        default="missing",
    )
    parser.add_argument("--no-merge-pdf-visuals", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
