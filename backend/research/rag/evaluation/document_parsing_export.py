from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from backend.foundation import PrimitiveModel
from backend.research.domain.document import ResearchDocument
from backend.research.domain.document_fidelity import DocumentBlock, DocumentFidelity


DOCUMENT_PARSING_EXPORT_VERSION = "research-document-production-export/v1"
# Bound report allocation: one page outcome is materialized for every source page.
MAX_EXPORTED_PAGES = 10_000


class ExportStatus(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    MISSING_OUTPUT = "missing_output"


class PageConvention(StrEnum):
    PHYSICAL_PDF_ONE_BASED = "physical_pdf_one_based"


class ContentKind(StrEnum):
    SECTION = "section"
    EQUATION = "equation"
    TABLE = "table"


class ExportedContent(PrimitiveModel):
    """A single item from the persisted ResearchDocument, never backend raw data."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: ContentKind
    identity: str
    content_id: str
    source_ref: str
    page: int | None
    page_end: int | None = None
    status: ExportStatus
    reason: str | None = None
    title: str | None = None
    text: str | None = None
    latex: str | None = None

    @model_validator(mode="after")
    def _content_matches_status(self) -> "ExportedContent":
        has_content = self.text is not None or self.latex is not None
        if self.status is ExportStatus.SUPPORTED and not has_content:
            raise ValueError("supported content must contain text or LaTeX")
        if self.status is not ExportStatus.SUPPORTED and has_content:
            raise ValueError("unavailable content must not expose a scoreable projection")
        if self.kind is ContentKind.EQUATION and self.latex is None and self.status is ExportStatus.SUPPORTED:
            raise ValueError("supported equations must preserve their LaTeX")
        if self.kind is not ContentKind.EQUATION and self.latex is not None:
            raise ValueError("only equations may export LaTeX")
        if self.kind is ContentKind.TABLE and self.text is not None:
            raise ValueError("legacy dict rows are not a complete table-grid export")
        return self


class PageExport(PrimitiveModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    page: int = Field(strict=True, ge=1, le=MAX_EXPORTED_PAGES)
    status: ExportStatus
    content_ids: tuple[str, ...] = ()
    reason: str | None = None


class UnsupportedDimension(PrimitiveModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    dimension: str
    reason: str


class ResearchDocumentParsingExport(PrimitiveModel):
    """Immutable, versioned evaluation view over persisted production document data."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["research-document-production-export/v1"] = DOCUMENT_PARSING_EXPORT_VERSION
    track: Literal["production_document"] = "production_document"
    document_id: str | None = None
    paper_id: str | None = None
    source_hash: str | None = None
    page_count: int | None = Field(default=None, strict=True, ge=1, le=MAX_EXPORTED_PAGES)
    # Preserve the immutable source-fidelity model without projecting it into
    # the lossy legacy sections/equations/tables arrays.
    fidelity: DocumentFidelity | None = None
    status: ExportStatus
    pages: tuple[PageExport, ...] = ()
    content: tuple[ExportedContent, ...] = ()
    unsupported_dimensions: tuple[UnsupportedDimension, ...] = ()
    supported_content_count: int = Field(default=0, strict=True, ge=0)
    unavailable_content_count: int = Field(default=0, strict=True, ge=0)
    missing_content_count: int = Field(default=0, strict=True, ge=0)
    missing_output_reason: str | None = None


def export_research_document(
    document: ResearchDocument | None,
    *,
    page_count: int | None,
    page_convention: PageConvention | None = None,
) -> ResearchDocumentParsingExport:
    """Export only faithfully page-locatable production content.

    ``page_count`` is the physical page count of the source PDF, supplied by the
    caller from source metadata. It must be an exact ``int`` (``bool`` is
    rejected) from 1 through ``MAX_EXPORTED_PAGES``; the cap bounds per-page
    report allocation, and larger inputs fail rather than being truncated.
    Page numbering is interpreted only when the caller explicitly declares a
    verified convention (currently physical,
    one-based PDF pages, matching the inspected Marker adapter). The exporter
    performs no page-index conversion, inference, or clamping.
    """
    if page_count is not None:
        if type(page_count) is not int:
            raise TypeError("page_count must be an exact int; bool and numeric coercion are not allowed")
        if not 1 <= page_count <= MAX_EXPORTED_PAGES:
            raise ValueError(f"page_count must be between 1 and {MAX_EXPORTED_PAGES}; export limit exceeded")
    if page_convention is not None and type(page_convention) is not PageConvention:
        raise TypeError("page_convention must be a PageConvention enum value or None")

    unsupported = (
        UnsupportedDimension(
            dimension="reading_order",
            reason=(
                "ResearchDocument stores sections, equations, and tables in separate arrays "
                "without a shared persisted block order; category-local array order is not a "
                "faithful mixed-content reading order."
            ),
        ),
        UnsupportedDimension(
            dimension="table_grid",
            reason=(
                "ResearchTable.rows is a dict-row projection and does not preserve a complete "
                "cell grid, spans, blank/covered cells, or cell locators."
            ),
        ),
    )
    if document is None:
        return ResearchDocumentParsingExport(
            page_count=page_count,
            status=ExportStatus.MISSING_OUTPUT,
            pages=tuple(
                PageExport(page=page, status=ExportStatus.MISSING_OUTPUT, reason="document_missing")
                for page in range(1, (page_count or 0) + 1)
            ),
            unsupported_dimensions=unsupported,
            missing_output_reason="ResearchDocument is missing; no other content source is accepted.",
        )

    if document.fidelity is not None:
        fidelity = document.fidelity
        unavailable_reason = None
        if fidelity.source_hash != document.source_hash:
            unavailable_reason = "fidelity_source_hash_mismatch"
        elif page_convention is not PageConvention.PHYSICAL_PDF_ONE_BASED:
            unavailable_reason = "fidelity_page_convention_unverified"
        elif page_count is None:
            unavailable_reason = "fidelity_page_count_unknown"
        elif page_count != len(fidelity.pages):
            unavailable_reason = "fidelity_page_count_mismatch"
        elif tuple(page.pdf_page_number for page in fidelity.pages) != tuple(range(1, len(fidelity.pages) + 1)):
            unavailable_reason = "fidelity_page_numbering_mismatch"

        if unavailable_reason is not None:
            return ResearchDocumentParsingExport(
                document_id=document.document_id,
                paper_id=document.paper_id,
                source_hash=document.source_hash,
                page_count=page_count,
                status=ExportStatus.UNAVAILABLE,
                pages=tuple(
                    PageExport(page=page, status=ExportStatus.UNAVAILABLE, reason=unavailable_reason)
                    for page in range(1, (page_count or 0) + 1)
                ),
                unsupported_dimensions=(
                    UnsupportedDimension(dimension="fidelity", reason=unavailable_reason),
                ),
            )

        blocks_by_page: dict[int, list[DocumentBlock]] = {
            page.pdf_page_number: [] for page in fidelity.pages
        }
        for block in fidelity.blocks:
            blocks_by_page[block.page_number].append(block)
        pages = tuple(
            PageExport(
                page=page.pdf_page_number,
                status=(
                    ExportStatus.SUPPORTED
                    if blocks_by_page[page.pdf_page_number]
                    else ExportStatus.MISSING_OUTPUT
                ),
                content_ids=tuple(
                    block.block_id
                    for block in sorted(
                        blocks_by_page[page.pdf_page_number], key=lambda item: item.reading_order
                    )
                ),
                reason=(
                    None
                    if blocks_by_page[page.pdf_page_number]
                    else "page_has_no_fidelity_blocks"
                ),
            )
            for page in fidelity.pages
        )
        return ResearchDocumentParsingExport(
            document_id=document.document_id,
            paper_id=document.paper_id,
            source_hash=document.source_hash,
            page_count=len(fidelity.pages),
            fidelity=fidelity,
            status=ExportStatus.SUPPORTED,
            pages=pages,
            supported_content_count=len(fidelity.blocks),
            missing_content_count=sum(not blocks_by_page[page.pdf_page_number] for page in fidelity.pages),
        )

    identities = [
        *((ContentKind.SECTION, section.section_id) for section in document.sections),
        *((ContentKind.EQUATION, equation.equation_id) for equation in document.equations),
        *((ContentKind.TABLE, table.table_id) for table in document.tables),
    ]
    seen_identities: set[tuple[ContentKind, str]] = set()
    duplicate_identities: set[tuple[ContentKind, str]] = set()
    for identity_key in identities:
        if identity_key in seen_identities:
            duplicate_identities.add(identity_key)
        seen_identities.add(identity_key)
    if duplicate_identities:
        duplicate_names = ", ".join(
            f"{kind.value}:{identity}"
            for kind, identity in sorted(duplicate_identities, key=lambda item: (item[0].value, item[1]))
        )
        raise ValueError(f"ResearchDocument contains duplicate same-kind content identities: {duplicate_names}")

    items: list[ExportedContent] = []
    by_page: dict[int, list[str]] = {page: [] for page in range(1, (page_count or 0) + 1)}
    item_status: dict[str, ExportStatus] = {}

    def resolve_page(
        source_ref: str,
        page: int | None,
        page_end: int | None = None,
        *,
        section: bool = False,
    ) -> tuple[ExportStatus, str | None, int | None]:
        if page is None:
            return ExportStatus.UNAVAILABLE, "page_missing", None
        if page_convention is None:
            return ExportStatus.UNAVAILABLE, "page_convention_unknown", page
        if page < 1:
            return ExportStatus.UNAVAILABLE, "page_out_of_range", page
        if page_count is None:
            return ExportStatus.UNAVAILABLE, "page_count_unknown", page
        if page > page_count or (page_end is not None and (page_end < page or page_end > page_count)):
            return ExportStatus.UNAVAILABLE, "page_out_of_range", page
        if section and page_end is None:
            return ExportStatus.UNAVAILABLE, "section_page_end_missing", page
        if page_end is not None and page_end != page:
            return ExportStatus.UNAVAILABLE, "cross_page_section_not_splittable", page
        if not source_ref.strip():
            return ExportStatus.UNAVAILABLE, "source_locator_missing", page
        return ExportStatus.SUPPORTED, None, page

    for section in document.sections:
        identity = section.section_id
        page = section.page_start
        page_end = section.page_end
        status, reason, resolved_page = resolve_page(section.source_ref, page, page_end, section=True)
        text = section.text if status is ExportStatus.SUPPORTED and section.text else None
        if status is ExportStatus.SUPPORTED and text is None:
            status, reason = ExportStatus.MISSING_OUTPUT, "empty_section_text"
        item = ExportedContent(
            kind=ContentKind.SECTION,
            identity=identity,
            content_id=f"{ContentKind.SECTION.value}:{identity}",
            source_ref=section.source_ref,
            page=resolved_page,
            page_end=page_end,
            status=status,
            reason=reason,
            title=section.title,
            text=text,
        )
        items.append(item)
        item_status[item.content_id] = item.status
        if item.page is not None and 1 <= item.page <= (page_count or 0):
            last_page = (
                page_end
                if reason == "cross_page_section_not_splittable" and page_end is not None
                else item.page
            )
            for covered_page in range(item.page, min(last_page, page_count or 0) + 1):
                by_page[covered_page].append(item.content_id)

    for equation in document.equations:
        status, reason, resolved_page = resolve_page(equation.source_ref, equation.page)
        latex = equation.latex if status is ExportStatus.SUPPORTED and equation.latex else None
        if status is ExportStatus.SUPPORTED and latex is None:
            status, reason = ExportStatus.MISSING_OUTPUT, "empty_equation_latex"
        item = ExportedContent(
            kind=ContentKind.EQUATION,
            identity=equation.equation_id,
            content_id=f"{ContentKind.EQUATION.value}:{equation.equation_id}",
            source_ref=equation.source_ref,
            page=resolved_page,
            status=status,
            reason=reason,
            latex=latex,
        )
        items.append(item)
        item_status[item.content_id] = item.status
        if item.page is not None and 1 <= item.page <= (page_count or 0):
            by_page[item.page].append(item.content_id)

    for table in document.tables:
        status, reason, resolved_page = resolve_page(table.source_ref, table.page)
        if status is ExportStatus.SUPPORTED:
            status, reason = ExportStatus.UNAVAILABLE, "table_grid_unavailable_dict_rows_are_lossy"
        item = ExportedContent(
            kind=ContentKind.TABLE,
            identity=table.table_id,
            content_id=f"{ContentKind.TABLE.value}:{table.table_id}",
            source_ref=table.source_ref,
            page=resolved_page,
            status=status,
            reason=reason,
            title=table.caption,
        )
        items.append(item)
        item_status[item.content_id] = item.status
        if item.page is not None and 1 <= item.page <= (page_count or 0):
            by_page[item.page].append(item.content_id)

    pages = tuple(
        PageExport(
            page=page,
            status=(
                ExportStatus.UNAVAILABLE
                if any(item_status[identity] is not ExportStatus.SUPPORTED for identity in by_page[page])
                else ExportStatus.SUPPORTED if by_page[page] else ExportStatus.MISSING_OUTPUT
            ),
            content_ids=tuple(by_page[page]),
            reason=(
                "page_contains_unavailable_document_content"
                if any(item_status[identity] is not ExportStatus.SUPPORTED for identity in by_page[page])
                else None if by_page[page] else "no_faithfully_exportable_document_content"
            ),
        )
        for page in range(1, (page_count or 0) + 1)
    )
    supported_count = sum(item.status is ExportStatus.SUPPORTED for item in items)
    unavailable_count = sum(item.status is ExportStatus.UNAVAILABLE for item in items)
    missing_count = sum(item.status is ExportStatus.MISSING_OUTPUT for item in items)
    if not items:
        status = ExportStatus.MISSING_OUTPUT
    elif supported_count == len(items) and all(page.status is ExportStatus.SUPPORTED for page in pages):
        status = ExportStatus.SUPPORTED
    elif supported_count:
        status = ExportStatus.PARTIAL
    else:
        status = ExportStatus.UNAVAILABLE
    return ResearchDocumentParsingExport(
        document_id=document.document_id,
        paper_id=document.paper_id,
        source_hash=document.source_hash,
        page_count=page_count,
        status=status,
        pages=pages,
        content=tuple(items),
        unsupported_dimensions=unsupported,
        supported_content_count=supported_count,
        unavailable_content_count=unavailable_count,
        missing_content_count=missing_count,
        missing_output_reason="document contains no body/equation/table output" if not items else None,
    )


__all__ = [
    "ContentKind",
    "DOCUMENT_PARSING_EXPORT_VERSION",
    "MAX_EXPORTED_PAGES",
    "ExportStatus",
    "ExportedContent",
    "PageConvention",
    "PageExport",
    "ResearchDocumentParsingExport",
    "UnsupportedDimension",
    "export_research_document",
]
