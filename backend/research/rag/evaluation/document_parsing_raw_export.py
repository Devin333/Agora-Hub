from __future__ import annotations

"""Lossless, read-only export of explicitly supported parser backend artifacts.

This module does not invoke a parser or scorer. It only validates and projects
already-persisted backend JSON for diagnostic evaluation.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence


class PageConvention(StrEnum):
    """Explicit source page encoding declared by a backend schema adapter."""

    PAGE_ID_ZERO_BASED = "page_id_zero_based"
    PAGE_INDEX_ZERO_BASED = "page_index_zero_based"
    PAGE_NUMBER_ONE_BASED = "page_number_one_based"


@dataclass(frozen=True, slots=True)
class BackendRawSchema:
    schema_id: str
    root_block_type: str
    page_block_type: str
    page_convention: PageConvention
    page_id_pattern: str | None = None
    page_field: str | None = None
    page_index_field: str | None = None
    container_block_types: frozenset[str] = frozenset()
    supported_block_types: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.schema_id.strip():
            raise ValueError("schema_id must be non-empty")
        if self.page_convention is PageConvention.PAGE_ID_ZERO_BASED:
            if self.page_id_pattern is None:
                raise ValueError("page_id_pattern is required for page-id convention")
            re.compile(self.page_id_pattern)
        elif self.page_convention is PageConvention.PAGE_INDEX_ZERO_BASED:
            if not self.page_index_field:
                raise ValueError("page_index_field is required for page-index convention")
        elif self.page_convention is PageConvention.PAGE_NUMBER_ONE_BASED:
            if not self.page_field:
                raise ValueError("page_field is required for page-number convention")


# Marker JSON produced by the project benchmark runner: Document -> Page nodes;
# /page/N/... uses a zero-based N. Containers are grouping nodes rather than
# content owners; semantic descendants (e.g. Table under TableGroup) own data.
MARKER_TREE_PAGE_ID_V1 = BackendRawSchema(
    schema_id="marker.document-tree.page-id-zero-based/v1",
    root_block_type="Document",
    page_block_type="Page",
    page_convention=PageConvention.PAGE_ID_ZERO_BASED,
    page_id_pattern=r"(?:^|/)page/(?P<page_index>\d+)/[A-Za-z][A-Za-z0-9]*/\d+$",
    container_block_types=frozenset({"Document", "Page", "TableGroup", "FigureGroup", "ListGroup", "TableCell"}),
    supported_block_types=frozenset({
        "Caption", "Code", "Equation", "Figure", "FigureCaption", "Handwriting",
        "Image", "Line", "List", "ListItem", "PageFooter", "PageHeader",
        "PageNumber", "Picture", "SectionHeader", "Table", "TableOfContents",
        "Text", "TextInlineEquation", "TextInlineMath", "Title",
    }),
)

# Explicit alternatives for other supported tree exports. These are separate
# schema identities so one-based and zero-based conventions are never inferred.
MARKER_TREE_PAGE_INDEX_V1 = BackendRawSchema(
    schema_id="marker.document-tree.page-idx-zero-based/v1",
    root_block_type="Document",
    page_block_type="Page",
    page_convention=PageConvention.PAGE_INDEX_ZERO_BASED,
    page_index_field="page_idx",
    container_block_types=MARKER_TREE_PAGE_ID_V1.container_block_types,
    supported_block_types=MARKER_TREE_PAGE_ID_V1.supported_block_types,
)
MARKER_TREE_PAGE_NUMBER_V1 = BackendRawSchema(
    schema_id="marker.document-tree.page-number-one-based/v1",
    root_block_type="Document",
    page_block_type="Page",
    page_convention=PageConvention.PAGE_NUMBER_ONE_BASED,
    page_field="page_number",
    container_block_types=MARKER_TREE_PAGE_ID_V1.container_block_types,
    supported_block_types=MARKER_TREE_PAGE_ID_V1.supported_block_types,
)


@dataclass(frozen=True, slots=True)
class SourcePage:
    """Identity of one requested input page, in the supplied batch order."""

    identity: str


class RawExportIssueCode(StrEnum):
    INVALID_ROOT = "invalid_root"
    INVALID_TREE = "invalid_tree"
    INVALID_PAGE_IDENTITY = "invalid_page_identity"
    MISSING_PAGE_IDENTITY = "missing_page_identity"
    CONFLICTING_PAGE_FIELDS = "conflicting_page_fields"
    PAGE_OUT_OF_RANGE = "page_out_of_range"
    MISSING_PAGE = "missing_page"
    BATCH_MAPPING_ERROR = "batch_mapping_error"
    UNKNOWN_BLOCK_FORMAT = "unknown_block_format"


@dataclass(frozen=True, slots=True)
class RawExportIssue:
    code: RawExportIssueCode
    message: str
    block_identity: str | None = None
    raw_page_values: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class RawBlockRecord:
    source_page_identity: str
    pdf_page_number: int
    backend_block_identity: str
    backend_block_type: str
    source_locator: str
    text: Any
    html: Any
    latex: Any


@dataclass(frozen=True, slots=True)
class RawPageRecord:
    source_page_identity: str
    pdf_page_number: int
    blocks: tuple[RawBlockRecord, ...]


@dataclass(frozen=True, slots=True)
class BackendRawExport:
    track: str
    schema_id: str
    artifact_ref: str
    pages: tuple[RawPageRecord, ...]
    issues: tuple[RawExportIssue, ...]
    status: str = "diagnostic"


def export_backend_raw(
    payload: Any,
    *,
    artifact_ref: str,
    input_pages: Sequence[SourcePage],
    schema: BackendRawSchema,
) -> BackendRawExport:
    """Project an existing backend tree without parsing, scoring or rewriting it.

    The caller supplies page identities in exact input-PDF order. Backend page
    identity is still required and validated; the sequence is used only to map
    an already decoded physical page number onto its corresponding source page.
    """
    issues: list[RawExportIssue] = []
    if not artifact_ref.strip():
        issues.append(RawExportIssue(RawExportIssueCode.BATCH_MAPPING_ERROR, "artifact_ref must be non-empty"))
    if not input_pages or any(not page.identity.strip() for page in input_pages):
        issues.append(RawExportIssue(RawExportIssueCode.BATCH_MAPPING_ERROR, "input page identities must be non-empty"))
    identities = [page.identity for page in input_pages]
    if len(set(identities)) != len(identities):
        issues.append(RawExportIssue(RawExportIssueCode.BATCH_MAPPING_ERROR, "input page identities must be unique"))

    output: list[list[RawBlockRecord]] = [[] for _ in input_pages]
    seen_pages: set[int] = set()
    if not isinstance(payload, Mapping) or payload.get("block_type") != schema.root_block_type:
        issues.append(RawExportIssue(
            RawExportIssueCode.INVALID_ROOT,
            f"expected object with block_type={schema.root_block_type!r}",
        ))
        return _result(schema, artifact_ref, input_pages, output, issues)
    roots = payload.get("children")
    if not isinstance(roots, list):
        issues.append(RawExportIssue(RawExportIssueCode.INVALID_TREE, "root children must be an array"))
        return _result(schema, artifact_ref, input_pages, output, issues)

    for page_node in roots:
        if not isinstance(page_node, Mapping) or page_node.get("block_type") != schema.page_block_type:
            issues.append(RawExportIssue(RawExportIssueCode.INVALID_TREE, "root children must be page nodes"))
            continue
        page_number, page_issue = _resolve_page(page_node, schema)
        page_identity = _identity(page_node)
        if page_issue is not None:
            issues.append(page_issue)
            continue
        assert page_number is not None
        if page_number < 1 or page_number > len(input_pages):
            issues.append(RawExportIssue(
                RawExportIssueCode.PAGE_OUT_OF_RANGE,
                f"physical page {page_number} is outside 1..{len(input_pages)}",
                page_identity,
            ))
            continue
        if page_number in seen_pages:
            issues.append(RawExportIssue(
                RawExportIssueCode.BATCH_MAPPING_ERROR,
                f"multiple backend page nodes map to physical page {page_number}",
                page_identity,
            ))
            continue
        seen_pages.add(page_number)
        if not _walk_page(
            page_node,
            page_number=page_number,
            source_page=identities[page_number - 1],
            artifact_ref=artifact_ref,
            schema=schema,
            output=output[page_number - 1],
            issues=issues,
        ):
            continue

    for page_number in range(1, len(input_pages) + 1):
        if page_number not in seen_pages:
            issues.append(RawExportIssue(
                RawExportIssueCode.MISSING_PAGE,
                f"no backend page node maps to physical page {page_number}",
            ))
    return _result(schema, artifact_ref, input_pages, output, issues)


def _resolve_page(
    node: Mapping[str, Any], schema: BackendRawSchema
) -> tuple[int | None, RawExportIssue | None]:
    identity = _identity(node)
    values: list[tuple[str, Any]] = []
    decoded: list[tuple[str, int]] = []
    if schema.page_convention is PageConvention.PAGE_ID_ZERO_BASED:
        raw_id = node.get("id")
        if isinstance(raw_id, str) and schema.page_id_pattern is not None:
            match = re.search(schema.page_id_pattern, raw_id)
            if match is not None:
                decoded.append(("id", int(match.group("page_index")) + 1))
            else:
                values.append(("id", raw_id))
        else:
            values.append(("id", raw_id))
    elif schema.page_convention is PageConvention.PAGE_INDEX_ZERO_BASED:
        raw = node.get(schema.page_index_field or "")
        values.append((schema.page_index_field or "page_idx", raw))
        parsed = _strict_integer(raw)
        if parsed is not None:
            decoded.append((schema.page_index_field or "page_idx", parsed + 1))
    elif schema.page_convention is PageConvention.PAGE_NUMBER_ONE_BASED:
        raw = node.get(schema.page_field or "")
        values.append((schema.page_field or "page_number", raw))
        parsed = _strict_integer(raw)
        if parsed is not None:
            decoded.append((schema.page_field or "page_number", parsed))

    # Any additional recognized page assertion must agree with the configured
    # convention. It is validation evidence, never a fallback.
    for key, offset in (("page", 0), ("page_number", 0), ("page_idx", 1), ("page_index", 1)):
        if key in node and not any(name == key for name, _ in values):
            raw = node.get(key)
            values.append((key, raw))
            parsed = _strict_integer(raw)
            if parsed is not None:
                decoded.append((key, parsed + offset))
    if schema.page_convention is not PageConvention.PAGE_ID_ZERO_BASED:
        raw_id = node.get("id")
        if isinstance(raw_id, str) and "/page/" in raw_id.casefold() and schema.page_id_pattern:
            match = re.search(schema.page_id_pattern, raw_id)
            if match:
                decoded.append(("id", int(match.group("page_index")) + 1))

    if not decoded:
        code = RawExportIssueCode.INVALID_PAGE_IDENTITY if values else RawExportIssueCode.MISSING_PAGE_IDENTITY
        return None, RawExportIssue(code, "page identity is absent or invalid for declared schema", identity, tuple(values))
    if len({number for _, number in decoded}) != 1:
        return None, RawExportIssue(
            RawExportIssueCode.CONFLICTING_PAGE_FIELDS,
            "page identity fields disagree under the declared schema convention",
            identity,
            tuple(values + decoded),
        )
    return decoded[0][1], None


def _walk_page(
    page_node: Mapping[str, Any], *, page_number: int, source_page: str,
    artifact_ref: str, schema: BackendRawSchema, output: list[RawBlockRecord],
    issues: list[RawExportIssue],
) -> bool:
    valid = True
    stack: list[Any] = list(reversed(page_node.get("children", [])))
    while stack:
        node = stack.pop()
        if not isinstance(node, Mapping):
            issues.append(RawExportIssue(RawExportIssueCode.INVALID_TREE, "block child must be an object"))
            valid = False
            continue
        kind = node.get("block_type")
        identity = _identity(node)
        children = node.get("children")
        if children is None:
            children = []
        if not isinstance(children, list):
            issues.append(RawExportIssue(RawExportIssueCode.INVALID_TREE, "block children must be an array", identity))
            valid = False
            continue
        # Every node's explicit page provenance, when present, must agree with
        # its containing page. Never inherit a page silently.
        node_valid = True
        explicit, issue = _resolve_page(node, schema)
        if issue is None and explicit != page_number:
            issues.append(RawExportIssue(
                RawExportIssueCode.CONFLICTING_PAGE_FIELDS,
                f"block page {explicit} conflicts with containing page {page_number}",
                identity,
            ))
            valid = False
            node_valid = False
        elif issue and issue.code not in (RawExportIssueCode.MISSING_PAGE_IDENTITY,):
            issues.append(issue)
            valid = False
            node_valid = False

        if not isinstance(kind, str) or not kind:
            issues.append(RawExportIssue(RawExportIssueCode.UNKNOWN_BLOCK_FORMAT, "block_type must be a non-empty string", identity))
            valid = False
            continue
        elif kind not in schema.container_block_types and kind not in schema.supported_block_types:
            issues.append(RawExportIssue(
                RawExportIssueCode.UNKNOWN_BLOCK_FORMAT,
                f"unsupported block_type {kind!r} for schema {schema.schema_id}",
                identity,
            ))
            valid = False
            continue
        elif kind not in schema.container_block_types and node_valid:
            if not identity:
                issues.append(RawExportIssue(RawExportIssueCode.UNKNOWN_BLOCK_FORMAT, "content node requires stable id", identity))
                valid = False
            elif _has_descendant_duplicate(node):
                # The deepest node owns repeated source content; preserving the
                # parent would create a second diagnostic copy.
                pass
            else:
                output.append(RawBlockRecord(
                    source_page_identity=source_page,
                    pdf_page_number=page_number,
                    backend_block_identity=identity,
                    backend_block_type=kind,
                    source_locator=f"{artifact_ref}#block={identity}",
                    text=node.get("text"),
                    html=node.get("html"),
                    latex=node.get("latex"),
                ))
        # Traversal retains backend order and exports semantic owner nodes only;
        # container summaries are not duplicated alongside their owned children.
        stack.extend(reversed(children))
    return valid


def _has_descendant_duplicate(node: Mapping[str, Any]) -> bool:
    signature = _content_signature(node)
    if signature is None:
        return False
    children = node.get("children")
    if not isinstance(children, list):
        return False
    for child in children:
        if not isinstance(child, Mapping):
            continue
        if _content_signature(child) == signature:
            return True
        if _has_descendant_duplicate(child):
            return True
    return False


def _content_signature(node: Mapping[str, Any]) -> tuple[Any, Any, Any] | None:
    values = (node.get("text"), node.get("html"), node.get("latex"))
    if not any(value is not None for value in values):
        return None
    return values


def _strict_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    return None


def _identity(node: Mapping[str, Any]) -> str | None:
    value = node.get("id")
    return value if isinstance(value, str) and value else None


def _result(
    schema: BackendRawSchema, artifact_ref: str, input_pages: Sequence[SourcePage],
    blocks: list[list[RawBlockRecord]], issues: list[RawExportIssue],
) -> BackendRawExport:
    pages = tuple(
        RawPageRecord(page.identity, index + 1, tuple(blocks[index]))
        for index, page in enumerate(input_pages)
    )
    return BackendRawExport("backend_raw", schema.schema_id, artifact_ref, pages, tuple(issues))
