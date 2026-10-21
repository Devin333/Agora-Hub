from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from backend.research.document.marker_table_grid import build_marker_table_grid
from backend.research.document.chunker import PaperDocumentChunker
from backend.research.document.marker_pdf_parser import (
    MarkerPdfDocumentParser,
    _block_provenance,
    _flatten_blocks,
    _flatten_pages,
    _heading_level,
    _html_table_rows,
    _html_to_text,
    _matrix_to_rows,
    _page_number,
    _page_reading_order,
)


def _pdf_bytes(page_count: int = 2, *, rotation: int = 0) -> bytes:
    document = pymupdf.open()
    for _ in range(page_count):
        page = document.new_page()
        if rotation:
            page.set_rotation(rotation)
    payload = document.tobytes()
    document.close()
    return payload


def _output_dir(command: list[str]) -> Path:
    for index, part in enumerate(command[:-1]):
        if part == "-v" and command[index + 1].endswith(":/output"):
            return Path(command[index + 1][:-len(":/output")])
    raise AssertionError("Marker output mount is missing")


def _parse_payload(
    monkeypatch: Any,
    tmp_path: Path,
    payload: dict[str, Any],
    *,
    pdf_bytes: bytes | None = None,
):
    monkeypatch.setenv("NEWSROOM_PARSER_RUN_ROOT", str(tmp_path / "parser-runs"))
    monkeypatch.setenv("NEWS_ARTIFACT_ROOT", str(tmp_path / "artifacts" / "runs"))
    raw_path: Path | None = None

    def fake_run(command: list[str], **_kwargs: Any) -> None:
        nonlocal raw_path
        output_dir = _output_dir(command)
        output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = output_dir / "input.json"
        raw_path.write_text(json.dumps(payload), encoding="utf-8")

    document = MarkerPdfDocumentParser(command_runner=fake_run).parse(
        "marker-provenance-test", pdf_bytes or _pdf_bytes()
    )
    assert raw_path is not None
    return document, raw_path


def _marker_weights_binding() -> dict[str, str]:
    root = Path(__import__("os").environ["NEWSROOM_MARKER_CACHE_DIR"]).resolve()
    manifest = json.loads(Path(__import__("os").environ["NEWSROOM_MARKER_WEIGHTS_MANIFEST"]).read_text())
    entries = sorted(manifest["files"], key=lambda entry: entry["path"])
    canonical = json.dumps({
        "schema": "newsroom-marker-weights-manifest.v1",
        "files": entries,
    }, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema": "newsroom-marker-weights-binding.v1",
        "host_cache_dir": str(root),
        "container_cache_dir": "/root/.cache",
        "verified_manifest_sha256": sha256(canonical).hexdigest(),
    }


def _declared_runner(*, bind_weights: bool = True) -> Any:
    class DeclaredRunner:
        def __call__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def cache_identity(self) -> dict[str, Any]:
            identity = {
                "schema": "research-parser-execution-adapter.v1",
                "profile": {"mode": "external_process"},
                "provider_identity": "test-provider/v1",
            }
            if bind_weights:
                identity["marker_weights_binding"] = _marker_weights_binding()
            return identity

    return DeclaredRunner()


def _configure_marker_cache_identity(
    monkeypatch: Any,
    tmp_path: Path,
    *,
    files: dict[str, bytes] | None = None,
    image: str | None = None,
) -> tuple[Path, Path]:
    root = tmp_path / "marker-cache"
    root.mkdir(parents=True, exist_ok=True)
    manifest_entries = []
    for relative, content in (files or {"models/layout/model.safetensors": b"weights-v1"}).items():
        target = root.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        manifest_entries.append({
            "path": relative,
            "sha256": sha256(content).hexdigest(),
            "size": len(content),
        })
    manifest = tmp_path / "marker-weights-manifest.json"
    manifest.write_text(json.dumps({
        "schema": "newsroom-marker-weights-manifest.v1",
        "files": manifest_entries,
    }), encoding="utf-8")
    monkeypatch.setenv(
        "NEWSROOM_MARKER_DOCKER_IMAGE",
        image or f"registry.example/newsroom-marker@sha256:{'1' * 64}",
    )
    monkeypatch.setenv("NEWSROOM_MARKER_CACHE_DIR", str(root.resolve()))
    monkeypatch.setenv("NEWSROOM_MARKER_WEIGHTS_MANIFEST", str(manifest.resolve()))
    monkeypatch.delenv("NEWSROOM_MARKER_DOCKER_ARGS", raising=False)
    return root, manifest


def test_cache_identity_rejects_unknown_execution_provider(monkeypatch: Any, tmp_path: Path) -> None:
    _configure_marker_cache_identity(monkeypatch, tmp_path)

    class UnknownProviderRunner:
        def __call__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def cache_identity(self) -> dict[str, Any]:
            identity = _declared_runner().cache_identity()
            identity["provider_identity"] = "unknown"
            return identity

    assert MarkerPdfDocumentParser(command_runner=UnknownProviderRunner()).cache_identity() is None


def test_cache_identity_fails_closed_without_fully_verified_opt_in(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    def fake_runner(*_args: Any, **_kwargs: Any) -> None:
        return None

    assert MarkerPdfDocumentParser().cache_identity() is None
    assert MarkerPdfDocumentParser(command_runner=fake_runner).cache_identity() is None
    assert MarkerPdfDocumentParser(command_runner=_declared_runner()).cache_identity() is None

    _root, manifest = _configure_marker_cache_identity(monkeypatch, tmp_path)
    monkeypatch.setenv("NEWSROOM_MARKER_DOCKER_IMAGE", "newsroom-marker:latest")
    assert MarkerPdfDocumentParser(command_runner=_declared_runner()).cache_identity() is None

    monkeypatch.setenv(
        "NEWSROOM_MARKER_DOCKER_IMAGE",
        f"registry.example/newsroom-marker@sha256:{'1' * 64}",
    )
    monkeypatch.setenv("NEWSROOM_MARKER_DOCKER_ARGS", "--entrypoint=/bin/sh")
    assert MarkerPdfDocumentParser(command_runner=_declared_runner()).cache_identity() is None

    monkeypatch.delenv("NEWSROOM_MARKER_DOCKER_ARGS")
    monkeypatch.delenv("NEWSROOM_MARKER_WEIGHTS_MANIFEST")
    assert MarkerPdfDocumentParser(command_runner=_declared_runner()).cache_identity() is None


def test_cache_identity_is_stable_for_verified_image_and_weights(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    _root, manifest = _configure_marker_cache_identity(monkeypatch, tmp_path, files={
        "models/layout/model.safetensors": b"weights-v1",
        "models/ocr/tokenizer.json": b"tokenizer-v1",
    })
    assert MarkerPdfDocumentParser(command_runner=_declared_runner(bind_weights=False)).cache_identity() is None
    parser = MarkerPdfDocumentParser(command_runner=_declared_runner())

    first = parser.cache_identity()
    manifest.write_text(json.dumps({
        "files": [
            {"size": len(b"tokenizer-v1"), "sha256": sha256(b"tokenizer-v1").hexdigest(),
             "path": "models/ocr/tokenizer.json"},
            {"size": len(b"weights-v1"), "sha256": sha256(b"weights-v1").hexdigest(),
             "path": "models/layout/model.safetensors"},
        ],
        "schema": "newsroom-marker-weights-manifest.v1",
    }, indent=2), encoding="utf-8")
    second = parser.cache_identity()

    assert first == second
    assert first == {
        "type": "backend.research.document.marker_pdf_parser.MarkerPdfDocumentParser",
        "execution_adapter": {
            "profile": {"mode": "external_process"},
            "provider_identity": "test-provider/v1",
            "schema": "research-parser-execution-adapter.v1",
            "marker_weights_binding": _marker_weights_binding(),
        },
        "marker_image": f"registry.example/newsroom-marker@sha256:{'1' * 64}",
        "marker_weights": {
            "schema": "newsroom-marker-weights-manifest.v1",
            "verified_manifest_sha256": first["marker_weights"]["verified_manifest_sha256"],
            "file_count": 2,
            "total_bytes": len(b"weights-v1") + len(b"tokenizer-v1"),
        },
    }


def test_parse_cache_fingerprint_tracks_marker_cache_identity_helper_source(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    from backend.research.application.parse_paper import _parse_cache_fingerprint
    from backend.research.document import marker_cache_identity

    _configure_marker_cache_identity(monkeypatch, tmp_path)
    parser = MarkerPdfDocumentParser(command_runner=_declared_runner())

    def fingerprint() -> str | None:
        return _parse_cache_fingerprint(parser, None, None, {}, "application/pdf")

    original = fingerprint()
    assert original is not None

    helper_path = Path(marker_cache_identity.__file__).resolve()
    original_read_bytes = Path.read_bytes

    def changed_helper_source(path: Path) -> bytes:
        content = original_read_bytes(path)
        if path.resolve() == helper_path:
            return content + b"\n# cache fingerprint regression\n"
        return content

    monkeypatch.setattr(Path, "read_bytes", changed_helper_source)
    assert fingerprint() != original


def test_cache_identity_changes_with_verified_image_or_weights(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    root, manifest = _configure_marker_cache_identity(monkeypatch, tmp_path)
    parser = MarkerPdfDocumentParser(command_runner=_declared_runner())
    initial = parser.cache_identity()
    assert initial is not None

    monkeypatch.setenv(
        "NEWSROOM_MARKER_DOCKER_IMAGE",
        f"registry.example/newsroom-marker@sha256:{'2' * 64}",
    )
    changed_image = parser.cache_identity()
    assert changed_image is not None
    assert changed_image != initial

    weights = root / "models" / "layout" / "model.safetensors"
    weights.write_bytes(b"weights-v2")
    assert parser.cache_identity() is None
    manifest.write_text(json.dumps({
        "schema": "newsroom-marker-weights-manifest.v1",
        "files": [{
            "path": "models/layout/model.safetensors",
            "sha256": sha256(b"weights-v2").hexdigest(),
            "size": len(b"weights-v2"),
        }],
    }), encoding="utf-8")
    changed_weights = parser.cache_identity()
    assert changed_weights is not None
    assert changed_weights != changed_image


def test_cache_identity_rejects_incomplete_or_unsafe_weights_tree(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    root, manifest = _configure_marker_cache_identity(monkeypatch, tmp_path)
    parser = MarkerPdfDocumentParser(command_runner=_declared_runner())
    assert parser.cache_identity() is not None

    (root / "unlisted.bin").write_bytes(b"not-in-manifest")
    assert parser.cache_identity() is None
    (root / "unlisted.bin").unlink()

    manifest.write_text(json.dumps({
        "schema": "newsroom-marker-weights-manifest.v1",
        "files": [{
            "path": "../outside.bin",
            "sha256": sha256(b"outside").hexdigest(),
            "size": len(b"outside"),
        }],
    }), encoding="utf-8")
    assert parser.cache_identity() is None


def test_cache_identity_rejects_oversized_and_growing_manifest(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    from backend.research.document import marker_cache_identity

    _root, manifest = _configure_marker_cache_identity(monkeypatch, tmp_path)
    parser = MarkerPdfDocumentParser(command_runner=_declared_runner())
    original_limit = marker_cache_identity._MARKER_WEIGHTS_MANIFEST_MAX_BYTES
    monkeypatch.setattr(marker_cache_identity, "_MARKER_WEIGHTS_MANIFEST_MAX_BYTES", 64)
    manifest.write_bytes(b" " * 65)
    assert parser.cache_identity() is None

    _configure_marker_cache_identity(monkeypatch, tmp_path)
    original_read = marker_cache_identity._read_bounded_fd
    grew = False

    def grow_before_read(file_descriptor: int, *, max_bytes: int) -> bytes | None:
        nonlocal grew
        if not grew:
            grew = True
            with manifest.open("ab") as stream:
                stream.write(b" ")
        return original_read(file_descriptor, max_bytes=max_bytes)

    monkeypatch.setattr(marker_cache_identity, "_read_bounded_fd", grow_before_read)
    assert parser.cache_identity() is None
    assert original_limit > 0


def test_cache_identity_rejects_weight_growth_during_hash(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    from backend.research.document import marker_cache_identity

    root, _manifest = _configure_marker_cache_identity(monkeypatch, tmp_path)
    weights = root / "models" / "layout" / "model.safetensors"
    original_read = marker_cache_identity.os.read
    grew = False

    def grow_during_read(file_descriptor: int, count: int) -> bytes:
        nonlocal grew
        if not grew and marker_cache_identity.os.fstat(file_descriptor).st_ino == weights.stat().st_ino:
            grew = True
            with weights.open("ab") as stream:
                stream.write(b"growth")
        return original_read(file_descriptor, count)

    monkeypatch.setattr(marker_cache_identity.os, "read", grow_during_read)
    assert MarkerPdfDocumentParser(command_runner=_declared_runner()).cache_identity() is None
    assert grew


def test_cache_identity_rejects_exact_file_symlink_in_weights_tree(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    root, manifest = _configure_marker_cache_identity(monkeypatch, tmp_path, files={
        "models/real.bin": b"real",
    })
    target = root / "models" / "real.bin"
    link = root / "models" / "linked.bin"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    manifest.write_text(json.dumps({
        "schema": "newsroom-marker-weights-manifest.v1",
        "files": [
            {"path": "models/real.bin", "sha256": sha256(b"real").hexdigest(), "size": 4},
            {"path": "models/linked.bin", "sha256": sha256(b"real").hexdigest(), "size": 4},
        ],
    }), encoding="utf-8")

    assert MarkerPdfDocumentParser(command_runner=_declared_runner()).cache_identity() is None


def test_flatten_preserves_nested_identity_position_order_and_duplicate_text() -> None:
    flattened = _flatten_blocks([
        {
            "type": "Document",
            "id": "doc-1",
            "children": [
                {"type": "Text", "id": "text-a", "text": "same"},
                {"type": "Text", "id": "text-b", "text": "same"},
            ],
        }
    ])

    assert [item["_marker_provenance"]["block_id"] for item in flattened] == [
        "doc-1", "text-a", "text-b"
    ]
    assert [item["_marker_provenance"]["parent_id"] for item in flattened] == [
        None, "doc-1", "doc-1"
    ]
    assert [item["_marker_provenance"]["child_position"] for item in flattened] == [
        0, 0, 1
    ]
    assert [item["_marker_provenance"]["source_order"] for item in flattened] == [0, 1, 2]
    assert [item["text"] for item in flattened if item.get("text")] == ["same", "same"]


def test_flatten_pages_uses_only_declared_page_context_and_keeps_full_coverage() -> None:
    blocks = _flatten_pages([
        {
            "page_idx": 0,
            "blocks": [{"type": "Page", "id": "page/0", "children": [
                {"type": "Text", "id": "page/0/text/1", "text": "first"},
                {"type": "Text", "id": "page/0/text/2", "text": "second"},
                {"type": "Text", "text": "context"},
            ]}],
        },
        {
            "page_idx": 1,
            "blocks": [{"type": "Text", "id": "page/1/text/1", "text": "third"}],
        },
        {"blocks": [{"type": "Text", "id": "no-page", "text": "unknown"}]},
    ])

    provenance = [block["_marker_provenance"] for block in blocks]
    assert [item["source_order"] for item in provenance] == list(range(len(blocks)))
    assert [item["block_id"] for item in provenance] == [
        "page/0", "page/0/text/1", "page/0/text/2", "marker:block:pages/0/0/2",
        "page/1/text/1", "no-page"
    ]
    assert _page_number(blocks[3]) == 1
    assert blocks[3]["_marker_provenance"]["block_id"] == "marker:block:pages/0/0/2"
    inherited_provenance = _block_provenance(blocks[3], page=1, locator="source#page=1")
    assert inherited_provenance["page"]["normalized"] == 1
    assert inherited_provenance["page"]["scheme"] == "page_idx:zero_based"
    assert inherited_provenance["page"]["value"] == {
        "field": "page_idx",
        "value": 0,
        "scheme": "page_idx:zero_based",
        "declared_on": "page_container",
    }
    assert blocks[-1].get("_marker_page_context") is None
    assert len({item["block_id"] for item in provenance}) == len(blocks)


def test_marker_provenance_tracks_pages_table_caption_owner_and_unsupported_raw(
    monkeypatch: Any, tmp_path: Path
) -> None:
    payload = {
        "pages": [
            {
                "page_idx": 0,
                "blocks": [{
                    "type": "Document",
                    "id": "doc-root",
                    "children": [{
                        "type": "Page",
                        "id": "page/0",
                        "children": [
                            {"type": "Text", "id": "page/0/text/1", "text": "repeat"},
                            {"type": "Text", "id": "page/0/text/2", "text": "repeat"},
                            {
                                "type": "Table",
                                "id": "page/0/table/1",
                                "caption": "Table 1: Scores",
                                "rows": [["Model", "Score"], ["A", "1"]],
                                "children": [
                                    {"type": "Caption", "id": "caption-1", "text": "Table 1: Scores"},
                                    {"type": "Table", "id": "nested-table", "rows": [["Model", "Score"]]},
                                ],
                            },
                            {"type": "NovelMarkerKind", "id": "unsupported-1", "text": "raw only"},
                        ],
                    }],
                }],
            },
            {
                "page_idx": 1,
                "blocks": [{"type": "Text", "id": "page/1/text/1", "text": "page two"}],
            },
        ]
    }

    document, raw_path = _parse_payload(monkeypatch, tmp_path, payload)

    text = "\n".join(section.text for section in document.sections)
    assert text.count("repeat") == 2
    section_provenance = document.sections[0].metadata["marker_provenance"]
    assert [block["block_id"] for block in section_provenance["blocks"]] == [
        "page/0/text/1", "page/0/text/2", "page/1/text/1"
    ]
    assert section_provenance["blocks"][0]["page"]["normalized"] == 1
    assert section_provenance["blocks"][2]["page"]["normalized"] == 2
    assert section_provenance["blocks"][0]["locator"].endswith("#page=1")
    assert section_provenance["blocks"][2]["locator"].endswith("#page=2")
    assert document.sections[0].page_start == 1
    assert document.sections[0].page_end == 2
    assert document.sections[0].metadata["page_range_complete"] is True

    assert len(document.tables) == 1
    table = document.tables[0]
    provenance = table.metadata["marker_provenance"]
    assert table.caption == "Table 1: Scores"
    assert provenance["block_id"] == "page/0/table/1"
    assert provenance["owner"] == {
        "block_id": "page/0/table/1",
        "kind": "table",
        "caption_of": None,
        "caption_block_ids": ["caption-1"],
    }
    assert provenance["locator"].endswith("#page=1")
    grid = table.grid
    assert grid is not None
    assert (grid.row_count, grid.column_count) == (2, 2)
    assert grid.has_header is False  # Matrix input has no explicit header marker.
    assert grid.cells[0].text == "Model"
    assert grid.cells[2].text == "A"
    reloaded_document = type(document).model_validate(document.model_dump(mode="json"))
    assert reloaded_document.tables[0].grid == grid

    assert document.fidelity is not None
    assert document.fidelity.source_hash == document.source_hash
    assert document.fidelity.content_checksum == "sha256:" + __import__("hashlib").sha256(
        raw_path.read_bytes()
    ).hexdigest()
    assert {block.page_number for block in document.fidelity.blocks} == {1, 2}
    table_block = next(block for block in document.fidelity.blocks if block.kind == "table")
    assert table_block.element_ref == table.table_id
    caption_block = next(block for block in document.fidelity.blocks if block.block_id == "caption-1")
    assert caption_block.content_owner_id == table_block.block_id
    assert table_block.parent_id == "page/0"

    warnings = document.metadata["parser_warnings"]
    assert any("unsupported Marker type 'novelmarkerkind'" in warning for warning in warnings)
    assert {item["code"] for item in document.metadata["parser_warning_codes"]} == {
        "unsupported_marker_type"
    }
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    assert raw == payload
    assert any(block.get("id") == "unsupported-1" for block in raw["pages"][0]["blocks"][0]["children"][0]["children"])


def test_marker_page_blocktype_inherits_zero_based_page_container_context(monkeypatch, tmp_path) -> None:
    document, _ = _parse_payload(monkeypatch, tmp_path, {
        "children": [{
            "id": "/page/0/Page/72",
            "block_type": "Page",
            "bbox": [0, 0, 1653, 2339],
            "children": [{
                "id": "/page/0/Line/2",
                "block_type": "Line",
                "text": "actual Marker page container",
                "bbox": [40, 50, 600, 90],
            }],
        }]
    })
    assert document.fidelity is not None
    line = next(block for block in document.fidelity.blocks if block.block_id == "/page/0/Line/2")
    assert line.page_number == 1
    assert line.bbox is None  # Marker emitted no declared coordinate schema.
    assert line.source_locator.endswith("#page=1")


def test_block_bbox_is_not_mistaken_for_page_container_bbox(monkeypatch, tmp_path) -> None:
    document, _ = _parse_payload(monkeypatch, tmp_path, {
        "pages": [{"page_idx": 0, "blocks": [{
            "type": "Text", "id": "page/0/text/1", "text": "unknown scale",
            "coordinate_schema": "marker_page_container_bbox_v1",
            "bbox": [10, 10, 100, 30],
        }]}]
    })
    assert document.fidelity is not None
    block = document.fidelity.blocks[0]
    assert block.bbox is None
    assert block.bbox_unknown_reason == "Marker block has no reliable geometry"


def test_marker_id_page_numbers_are_zero_based_and_never_guessed_or_clamped() -> None:
    flattened = _flatten_blocks([
        {"type": "Text", "id": "document/page/2/text/1", "text": "explicit"},
        {"type": "Text", "id": "no-page-id", "page_idx": -4, "text": "negative"},
        {"type": "Text", "id": "no-page-field", "text": "unknown"},
    ])
    assert flattened[0]["_marker_provenance"]["source_order"] == 0
    assert _page_number(flattened[0]) == 3
    assert _page_number(flattened[1]) == -3
    assert _page_number(flattened[2]) is None


def test_section_page_range_is_unknown_when_a_contributing_block_has_no_page(
    monkeypatch: Any, tmp_path: Path
) -> None:
    document, _ = _parse_payload(monkeypatch, tmp_path, {
        "pages": [{"blocks": [
            {"type": "Text", "id": "document/page/0/text/1", "text": "located"},
            {"type": "Text", "id": "no-page-id", "text": "unlocated"},
        ]}],
    })

    assert len(document.sections) == 1
    assert document.sections[0].page_start is None
    assert document.sections[0].page_end is None
    assert document.sections[0].metadata["page_range_complete"] is False


def test_marker_parses_each_pdf_source_independently_without_batch_concatenation(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEWSROOM_PARSER_RUN_ROOT", str(tmp_path / "parser-runs"))
    monkeypatch.setenv("NEWS_ARTIFACT_ROOT", str(tmp_path / "artifacts" / "runs"))
    payloads = iter((
        {"pages": [{"page_idx": 0, "blocks": [
            {"type": "Text", "id": "document/page/0/text/1", "text": "document A only"},
        ]}]},
        {"pages": [
            {"page_idx": 0, "blocks": [{"type": "Text", "id": "document/page/0/text/1", "text": "document B page one"}]},
            {"page_idx": 1, "blocks": [{"type": "Text", "id": "document/page/1/text/1", "text": "document B page two"}]},
        ]},
    ))

    def fake_run(command: list[str], **_kwargs: Any) -> None:
        output_dir = _output_dir(command)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "input.json").write_text(json.dumps(next(payloads)), encoding="utf-8")

    parser = MarkerPdfDocumentParser(command_runner=fake_run)
    document_a = parser.parse("paper-a", _pdf_bytes(page_count=1))
    document_b = parser.parse("paper-b", _pdf_bytes(page_count=2))

    assert document_a.source_hash != document_b.source_hash
    assert "document A only" in " ".join(section.text for section in document_a.sections)
    assert "document B" not in " ".join(section.text for section in document_a.sections)
    assert "document A" not in " ".join(section.text for section in document_b.sections)
    assert document_b.sections[0].page_start == 1
    assert document_b.sections[0].page_end == 2


def test_table_projection_preserves_explicit_blank_and_missing_slots() -> None:
    columns, rows = _matrix_to_rows([
        ["Metric", "Value", "Note"],
        ["accuracy", "", "reported blank"],
        ["recall", "0.91"],
    ])
    assert columns == ["Metric", "Value", "Note"]
    assert rows == [
        {"Metric": "accuracy", "Value": "", "Note": "reported blank"},
        {"Metric": "recall", "Value": "0.91", "Note": None},
    ]


def test_table_html_keeps_empty_rows_and_single_row_headers() -> None:
    columns, rows = _html_table_rows(
        "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
        "<tbody><tr><td></td><td>2</td></tr><tr><td>3</td></tr></tbody></table>"
    )
    assert columns == ["A", "B"]
    assert rows == [{"A": "", "B": "2"}, {"A": "3", "B": None}]
    assert _matrix_to_rows([["only", "headers"]]) == (["only", "headers"], [])


def test_marker_table_grid_preserves_spans_headers_blank_and_missing_cells() -> None:
    grid = build_marker_table_grid(
        {"html": (
            "<table><thead><tr><th colspan='2'>Group</th></tr>"
            "<tr><th>A</th><th>B</th></tr></thead><tbody>"
            "<tr><td rowspan='2'>x</td><td></td></tr><tr><td>2</td></tr>"
            "</tbody></table>"
        )},
        table_id="table-1",
        block_id="marker-table-1",
        source_locator="paper://paper-1/pdf#page=1",
    )
    assert grid is not None
    assert grid.has_header is True
    assert grid.header_paths[0].cell_ids == (grid.cells[0].cell_id, grid.cells[2].cell_id)
    assert grid.header_paths[1].cell_ids == (grid.cells[0].cell_id, grid.cells[3].cell_id)
    assert grid.cells[1].status == "covered"
    assert grid.cells[5].status == "blank"
    assert grid.cells[6].status == "covered"
    assert grid.cells[6].covered_by_cell_id == grid.cells[4].cell_id


def test_marker_table_grid_fails_closed_on_invalid_span() -> None:
    grid = build_marker_table_grid(
        {"html": "<table><tr><td colspan='0'>bad</td></tr></table>"},
        table_id="table-invalid",
        block_id="marker-table-invalid",
        source_locator="paper://paper-1/pdf#page=1",
    )
    assert grid is None
    oversized = build_marker_table_grid(
        {"html": "<table><tr><td rowspan='1000' colspan='1000'>huge</td></tr></table>"},
        table_id="table-huge",
        block_id="marker-table-huge",
        source_locator="paper://paper-1/pdf#page=1",
    )
    assert oversized is None


def test_marker_table_grid_retains_formula_source_and_excludes_escaped_delimiters() -> None:
    grid = build_marker_table_grid(
        {"html": r"<table><tr><td>value</td><td>$$x_i + \frac{a}{b}$$</td>"
                 r"<td>literal \$5 then $y$</td></tr></table>"},
        table_id="table-math",
        block_id="marker-table-math",
        source_locator="paper://paper-1/pdf#page=1",
    )
    assert grid is not None
    math_cell = grid.cells[1]
    assert math_cell.text == r"$$x_i + \frac{a}{b}$$"
    assert len(math_cell.inline_math) == 1
    assert math_cell.inline_math[0].raw_latex == r"x_i + \frac{a}{b}"
    assert math_cell.inline_math[0].display is True
    escaped_cell = grid.cells[2]
    assert escaped_cell.text == r"literal \$5 then $y$"
    assert [span.raw_latex for span in escaped_cell.inline_math] == ["y"]


def test_marker_fidelity_fingerprint_is_independent_of_staging_paths(monkeypatch, tmp_path) -> None:
    payload = {"pages": [{"page_idx": 0, "blocks": [{
        "type": "Text", "id": "page/0/text/1", "text": "Stable identity",
        "coordinate_schema": "marker_page_container_bbox_v1",
        "bbox": [10, 10, 200, 40],
    }]}]}
    first, _ = _parse_payload(monkeypatch, tmp_path / "first", payload)
    second, _ = _parse_payload(monkeypatch, tmp_path / "second", payload)
    assert first.fidelity is not None and second.fidelity is not None
    assert first.fidelity.parser_fingerprint == second.fidelity.parser_fingerprint
    assert first.fidelity.profile_fingerprint == second.fidelity.profile_fingerprint


def test_marker_html_text_excludes_script_style_and_template_payloads() -> None:
    assert _html_to_text(
        "<p onclick='steal()'>visible</p><script>alert(1)</script>"
        "<style>body{display:none}</style><template>hidden</template>safe"
    ) == "visible safe"


def test_table_html_grid_excludes_executable_payloads_and_attributes() -> None:
    grid = build_marker_table_grid(
        {"html": (
            "<table><tr><th onclick='run()'>Metric</th><th>Value</th></tr>"
            "<tr><td>score<script>exfiltrate()</script></td>"
            "<td><img src=x onerror='run()'>0.91</td></tr></table>"
        )},
        table_id="table-sanitized",
        block_id="marker-table-sanitized",
        source_locator="paper://paper-1/pdf#page=1",
    )
    assert grid is not None
    assert [cell.text for cell in grid.cells] == ["Metric", "Value", "score", "0.91"]
    assert all("run()" not in cell.text and "exfiltrate" not in cell.text for cell in grid.cells)


def test_marker_fidelity_keeps_display_equation_source_and_span(monkeypatch, tmp_path) -> None:
    document, _ = _parse_payload(monkeypatch, tmp_path, {
        "pages": [{"page_idx": 0, "blocks": [{
            "type": "Equation", "id": "page/0/equation/1", "latex": r"E=mc^2",
            "coordinate_schema": "marker_page_container_bbox_v1",
            "bbox": [10, 10, 200, 40],
        }]}]
    })
    assert document.fidelity is not None
    equation_block = next(block for block in document.fidelity.blocks if block.kind == "equation")
    assert equation_block.text == r"E=mc^2"
    assert equation_block.inline_math[0].raw_latex == r"E=mc^2"
    assert equation_block.inline_math[0].display is True


def test_marker_display_equation_preserves_explicit_formula_sources_verbatim(monkeypatch, tmp_path) -> None:
    raw_latex = r"x<y + \text{<tag>} + A&amp;B"
    raw_tex = r"\operatorname{score}<threshold + \text{<node>} + C&#38;D"
    raw_align = r"\begin{aligned}a_i &= b_i \\ c_i &< d_i\end{aligned}"
    raw_matrix = r"\begin{matrix}a_{ij}&b_{ij}\\c_{ij}&d_{ij}\end{matrix}"
    document, _ = _parse_payload(monkeypatch, tmp_path, {
        "pages": [{"page_idx": 0, "blocks": [
            {
                "type": "Equation", "id": "page/0/equation/latex",
                "latex": raw_latex, "html": "<p>wrong &amp; fallback</p>",
                "coordinate_schema": "marker_page_container_bbox_v1",
                "bbox": [10, 10, 200, 40],
            },
            {
                "block_type": "Equation", "id": "page/0/equation/tex",
                "latex": " \t", "tex": raw_tex, "text": "wrong text fallback",
                "coordinate_schema": "marker_page_container_bbox_v1",
                "bbox": [10, 50, 300, 80],
            },
            {
                "block_type": "Equation", "id": "page/0/equation/align",
                "latex": raw_align,
                "coordinate_schema": "marker_page_container_bbox_v1",
                "bbox": [10, 90, 300, 130],
            },
            {
                "block_type": "Equation", "id": "page/0/equation/matrix",
                "latex": raw_matrix,
                "coordinate_schema": "marker_page_container_bbox_v1",
                "bbox": [10, 140, 300, 180],
            },
        ]}]
    })

    expected_sources = [raw_latex, raw_tex, raw_align, raw_matrix]
    assert [equation.latex for equation in document.equations] == expected_sources
    assert document.fidelity is not None
    equation_blocks = {
        block.block_id: block for block in document.fidelity.blocks if block.kind == "equation"
    }
    for block_id, expected in (
        ("page/0/equation/latex", raw_latex),
        ("page/0/equation/tex", raw_tex),
        ("page/0/equation/align", raw_align),
        ("page/0/equation/matrix", raw_matrix),
    ):
        block = equation_blocks[block_id]
        assert block.text == expected
        assert len(block.inline_math) == 1
        span = block.inline_math[0]
        assert span.raw_latex == expected
        assert span.display is True
        assert span.offset_text_version == "raw"
        assert (span.start_offset, span.end_offset) == (0, len(expected))
        assert block.text[span.start_offset:span.end_offset] == span.raw_latex


def test_marker_display_equation_text_content_and_html_fallbacks_keep_html_semantics(
    monkeypatch, tmp_path,
) -> None:
    fallback_sources = (
        ("text", "<p><span>x &lt; y &amp; z</span></p>", "x < y & z"),
        ("content", "<div>a &lt; b</div>", "a < b"),
        ("html", "<p>c &gt; d</p>", "c > d"),
    )
    blocks = [{
        "type": "Equation", "id": f"page/0/equation/{field}",
        field: source,
        "coordinate_schema": "marker_page_container_bbox_v1",
        "bbox": [10, 10 + index * 40, 200, 40 + index * 40],
    } for index, (field, source, _) in enumerate(fallback_sources)]
    document, _ = _parse_payload(monkeypatch, tmp_path, {
        "pages": [{"page_idx": 0, "blocks": blocks}]
    })

    expected = [text for _, _, text in fallback_sources]
    assert [equation.latex for equation in document.equations] == expected
    assert document.fidelity is not None
    equation_blocks = [block for block in document.fidelity.blocks if block.kind == "equation"]
    assert [block.text for block in equation_blocks] == expected
    for equation_block, text in zip(equation_blocks, expected):
        assert equation_block.inline_math[0].raw_latex == text
        assert equation_block.inline_math[0].end_offset == len(text)


def _reading_order_blocks(boxes):
    return [
        ({"type": "Text", "_marker_provenance": {"block_id": str(i)}}, box)
        for i, box in enumerate(boxes)
    ]


def _reading_order_ids(result):
    return [key for key, _ in sorted(result.items(), key=lambda item: item[1][0])]


def test_marker_two_column_order_uses_vertical_bands_and_stable_ties() -> None:
    blocks = _reading_order_blocks([
        (330, 10, 580, 30), (20, 10, 270, 30),
        (330, 50, 580, 70), (20, 50, 270, 70),
        (10, 90, 602, 120),  # spanning section heading separates bands
        (330, 130, 580, 150), (20, 130, 270, 150),
    ])
    result = _page_reading_order(blocks, pymupdf.Rect(0, 0, 612, 792))
    assert _reading_order_ids(result) == ["1", "3", "0", "2", "4", "6", "5"]
    assert sorted(order for order, _, _ in result.values()) == list(range(len(blocks)))
    assert result["4"] == (4, "preserved", None)
    assert all(reason == "high_confidence_two_column_vertical_bands"
               for _, status, reason in result.values() if status == "reordered")


def test_marker_rotated_page_is_uncertain() -> None:
    blocks = _reading_order_blocks([
        (350, 10, 580, 30), (30, 10, 260, 30),
        (350, 50, 580, 70), (30, 50, 260, 70),
    ])
    result = _page_reading_order(
        blocks, pymupdf.Rect(0, 0, 612, 792), page_rotation=90,
    )
    assert _reading_order_ids(result) == ["0", "1", "2", "3"]
    assert all(status == "uncertain" and reason == "rotated_page_layout_unsupported"
               for _, status, reason in result.values())


def test_marker_two_column_same_y_uses_x_then_source_order() -> None:
    blocks = _reading_order_blocks([
        (350, 20, 580, 40), (30, 20, 260, 40),
        (350, 20, 580, 40), (30, 20, 260, 40),
    ])
    result = _page_reading_order(blocks, pymupdf.Rect(0, 0, 612, 792))
    assert _reading_order_ids(result) == ["1", "3", "0", "2"]


def test_marker_single_column_and_insufficient_candidate_keep_backend_order() -> None:
    blocks = _reading_order_blocks([(30, 10, 500, 30), (30, 50, 500, 70),
                                    (30, 90, 500, 110), (30, 130, 500, 150)])
    result = _page_reading_order(blocks, pymupdf.Rect(0, 0, 612, 792))
    assert _reading_order_ids(result) == ["0", "1", "2", "3"]
    assert all(status == "uncertain" and reason for _, status, reason in result.values())


def test_marker_two_column_rejects_wide_threshold_and_missing_bbox() -> None:
    normal = [(350, 10, 580, 30), (30, 10, 260, 30),
              (350, 50, 580, 70), (30, 50, 260, 70)]
    too_wide = normal[:]
    too_wide[0] = (220, 10, 590, 30)
    for boxes, expected in ((too_wide, "ambiguous_column_overlap"),
                            (normal[:3] + [None], "layout_geometry_unavailable")):
        result = _page_reading_order(_reading_order_blocks(boxes), pymupdf.Rect(0, 0, 612, 792))
        assert _reading_order_ids(result) == ["0", "1", "2", "3"]
        assert all(status == "uncertain" and reason == expected for _, status, reason in result.values())


def test_marker_two_column_rejects_conflicting_geometry_and_keeps_contiguous_order() -> None:
    boxes = [(350, 10, 580, 30), (30, 10, 260, 30),
             (350, 50, 580, 70), (30, 50, 260, 70),
             (250, 30, 360, 55)]
    result = _page_reading_order(_reading_order_blocks(boxes), pymupdf.Rect(0, 0, 612, 792))
    assert _reading_order_ids(result) == ["0", "1", "2", "3", "4"]
    assert all(status == "uncertain" and reason == "ambiguous_column_overlap"
               for _, status, reason in result.values())


def test_marker_converter_rotation_marks_candidate_uncertain_and_preserves_order(monkeypatch, tmp_path) -> None:
    payload = {"pages": [{
        "page_idx": 0,
        "coordinate_schema": "marker_page_container_bbox_v1",
        "bbox": [0, 0, 792, 612],
        "blocks": [
            {"type": "Text", "id": "right-top", "text": "right top", "bbox": [430, 10, 760, 30]},
            {"type": "Text", "id": "left-top", "text": "left top", "bbox": [30, 10, 360, 30]},
            {"type": "Text", "id": "right-bottom", "text": "right bottom", "bbox": [430, 50, 760, 70]},
            {"type": "Text", "id": "left-bottom", "text": "left bottom", "bbox": [30, 50, 360, 70]},
        ],
    }]}
    document, _ = _parse_payload(
        monkeypatch, tmp_path, payload, pdf_bytes=_pdf_bytes(rotation=90),
    )
    assert document.fidelity is not None
    fidelity_order = sorted(document.fidelity.blocks, key=lambda block: block.reading_order)
    assert [block.block_id for block in fidelity_order] == [
        "right-top", "left-top", "right-bottom", "left-bottom",
    ]
    candidate = document.metadata["reading_order_candidates"][0]
    assert candidate["source_ids"] == [block.block_id for block in fidelity_order]
    assert candidate["candidate_ids"] == candidate["source_ids"]
    assert candidate["admission"] == "uncertain"
    assert candidate["reason"] == "rotated_page_layout_unsupported"


def test_marker_candidate_preserves_non_candidate_slots() -> None:
    blocks = _reading_order_blocks([
        (350, 10, 580, 30), (30, 10, 260, 30),
        (350, 50, 580, 70), (30, 50, 260, 70),
    ])
    blocks.insert(1, ({"type": "Unknown", "_marker_provenance": {"block_id": "other"}}, None))
    result = _page_reading_order(blocks, pymupdf.Rect(0, 0, 612, 792))
    assert _reading_order_ids(result) == ["1", "other", "3", "0", "2"]
    assert sorted(order for order, _, _ in result.values()) == list(range(len(blocks)))


def test_marker_converter_records_candidate_but_keeps_backend_fidelity_and_chunk_order(monkeypatch, tmp_path) -> None:
    payload = {"pages": [{
        "page_idx": 0,
        "coordinate_schema": "marker_page_container_bbox_v1",
        "bbox": [0, 0, 612, 792],
        "blocks": [
            {"type": "Text", "id": "right-top", "text": "right top", "bbox": [330, 10, 580, 30]},
            {"type": "Text", "id": "left-top", "text": "left top", "bbox": [20, 10, 270, 30]},
            {"type": "Text", "id": "right-bottom", "text": "right bottom", "bbox": [330, 50, 580, 70]},
            {"type": "Text", "id": "left-bottom", "text": "left bottom", "bbox": [20, 50, 270, 70]},
        ],
    }]}
    document, _ = _parse_payload(monkeypatch, tmp_path, payload)

    assert document.fidelity is not None
    fidelity_order = sorted(document.fidelity.blocks, key=lambda block: block.reading_order)
    source_ids = [block["id"] for block in payload["pages"][0]["blocks"]]
    assert [block.block_id for block in fidelity_order] == source_ids
    assert [block.reading_order for block in fidelity_order] == list(range(len(source_ids)))
    candidate = document.metadata["reading_order_candidates"][0]
    assert candidate["page"] == 1
    assert candidate["source_ids"] == source_ids
    assert candidate["candidate_ids"] == ["left-top", "left-bottom", "right-top", "right-bottom"]
    assert candidate["admission"] == "candidate"
    assert candidate["reason"] == "high_confidence_two_column_vertical_bands"
    assert document.metadata["reading_order_candidates_omitted"] == {
        "pages": 0, "source_ids": 0, "candidate_ids": 0,
    }

    chunks = PaperDocumentChunker().chunk(document, "marker")
    chunk_text = "\n".join(chunk.content for chunk in chunks)
    offsets = [chunk_text.index(text) for text in ("right top", "left top", "right bottom", "left bottom")]
    assert offsets == sorted(offsets)


def test_marker_fidelity_is_unavailable_when_any_source_block_has_unknown_page(monkeypatch, tmp_path) -> None:
    payload = {"blocks": [
        {"type": "Text", "id": "known", "page_idx": 0, "text": "known",
         "coordinate_schema": "marker_page_container_bbox_v1",
         "page_bbox": [0, 0, 612, 792], "bbox": [10, 10, 100, 30]},
        {"type": "Text", "id": "unknown-page", "text": "unlocated"},
    ]}
    document, _ = _parse_payload(monkeypatch, tmp_path, payload)
    assert document.fidelity is None
    assert document.metadata["fidelity_status"] == "unavailable"
    assert document.metadata["fidelity_unavailable_reason"]


def test_marker_heading_level_uses_explicit_section_hierarchy_without_coercing_bools() -> None:
    assert _heading_level({"section_hierarchy": {"1": "root", "3": "leaf"}}) == 3
    assert _heading_level({"heading_level": True, "section_hierarchy": {"2": "nested"}}) == 2
    assert _heading_level({"heading_level": "4"}) == 4
    assert _heading_level({"heading_level": 0}) == 1


def test_marker_missing_formula_emits_structured_warning_code(monkeypatch, tmp_path) -> None:
    document, _ = _parse_payload(monkeypatch, tmp_path, {
        "children": [{
            "id": "/page/0/Page/72",
            "block_type": "Page",
            "bbox": [0, 0, 612, 792],
            "children": [{
                "id": "/page/0/Equation/1",
                "block_type": "Equation",
                "html": '<p block-type="Equation"></p>',
                "bbox": [100, 200, 400, 240],
            }],
        }]
    })

    assert document.metadata["parser_warning_codes"] == [
        {"code": "formula_source_missing", "block_index": 1}
    ]
    assert document.metadata["fidelity_status"] == "unavailable"


def test_duplicate_headers_do_not_overwrite_literal_suffix_columns() -> None:
    columns, rows = _matrix_to_rows([
        ["A", "A", "A_2", "A", "A_3"],
        ["first", "second", "third", "fourth", "fifth"],
    ])
    assert columns == ["A", "A_4", "A_2", "A_5", "A_3"]
    assert list(rows[0].values()) == ["first", "second", "third", "fourth", "fifth"]
    assert len(rows[0]) == 5


def test_generated_blank_column_names_do_not_overwrite_explicit_headers() -> None:
    columns, rows = _matrix_to_rows([
        ["", "column_1", "column_1_2"], ["blank-header", "explicit", "suffix"],
    ])
    assert len(set(columns)) == 3
    assert list(rows[0].values()) == ["blank-header", "explicit", "suffix"]


