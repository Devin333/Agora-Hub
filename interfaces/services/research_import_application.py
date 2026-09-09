from __future__ import annotations

from typing import Any

from interfaces.services.research_import_service import ResearchImportService
from interfaces.services.research_service import ResearchActorInput, ResearchApplicationService, ResearchParseInput, ResearchServiceError


class ResearchImportApplication:
    """Convert a received, owned import and durably record its actual outcome."""

    def __init__(self, imports: ResearchImportService, research: ResearchApplicationService) -> None:
        self._imports = imports
        self._research = research

    def process(self, *, user_id: str, import_id: str) -> dict[str, Any]:
        with self._imports.processing(user_id=user_id, import_id=import_id) as record:
            if record["status"] == "completed":
                return record
            self._imports.mark_parsing(user_id=user_id, import_id=import_id)
            try:
                result = self._research.parse_paper(ResearchParseInput(
                    source=self._imports.source_path(user_id=user_id, import_id=import_id).as_uri(),
                    source_type="local",
                    options={"quality_profile": "reading", "include_catalog": True, "include_chunks": True},
                    metadata={"import_id": import_id},
                    user_id=user_id,
                ))
                paper_id = result.get("paperId") or result.get("paper_id")
                if not paper_id:
                    raise ResearchServiceError("import_document_missing", "转换未生成可阅读的论文", status_code=502)
                payload = self._research.get_document(str(paper_id), actor=ResearchActorInput(user_id=user_id))
                document = payload.get("document") or {}
                if document.get("status") != "parsed" or not document.get("sections") or not (result.get("qualityReport") or {}).get("passed"):
                    raise ResearchServiceError("import_quality_failed", "正文转换未通过质量检查", status_code=422)
                return self._imports.mark_completed(user_id=user_id, import_id=import_id, result=result)
            except ResearchServiceError as exc:
                message = (
                    "未提取到足够清晰的正文。请使用未加密、可选中文字的 PDF；扫描件暂不支持。"
                    if exc.code in {"import_quality_failed", "import_document_missing"}
                    else "PDF 转换没有完成，请重试。"
                )
                return self._imports.mark_failed(user_id=user_id, import_id=import_id, code=exc.code, message=message)
            except Exception:
                return self._imports.mark_failed(user_id=user_id, import_id=import_id, code="research_parse_failed", message="PDF 转换暂时不可用，请稍后重试。")
