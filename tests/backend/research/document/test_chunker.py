from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from framework.shared.windows_paths import storage_io_path

from backend.research.document.chunk_manifest import ChunkManifestManager
from backend.research.document.chunker import PaperDocumentChunker, _fidelity_chunk_source_map
from backend.research.document.models import PaperChunk
from backend.research.domain.document_fidelity import (
    DocumentBlock, DocumentFidelity, DocumentPage, InlineMathSpan, TableCell, TableGrid,
)
from backend.research.document.section_detector import classify_section_role, is_abstract_section
from backend.research.document.special_element_scanner import scan_special_elements
from backend.research.rag.retrieval.paper_policy import build_retrieval_route, classify_query_intent
from tests.backend.research.document.helpers import (
    make_doc,
    make_equation,
    make_figure,
    make_section,
    make_table,
)

CHUNKER = PaperDocumentChunker()


# ── section_detector ─────────────────────────────────────────────────────────

def test_is_abstract_section():
    assert is_abstract_section("Abstract")
    assert is_abstract_section("摘要")
    assert not is_abstract_section("Introduction")


@pytest.mark.parametrize("title,expected_role", [
    ("Experiments", "experiment"),
    ("Method", "method"),
    ("Related Work", "related_work"),
    ("Conclusion", "conclusion"),
    ("Introduction", "background"),
    ("Analysis", "analysis"),
])
def test_classify_section_role(title, expected_role):
    roles = classify_section_role(title)
    assert expected_role in roles


# ── special_element_scanner ───────────────────────────────────────────────────

def test_scan_figures_and_equations():
    doc = make_doc(sections=[
        make_section("s1", "Introduction", "As shown in 图 fig1, results are great.")
    ])
    doc2 = doc.model_copy(update={
        "figures": [make_figure("fig1", "Our proposed architecture.")],
        "equations": [make_equation("eq1", r"\mathcal{L} = \sum_i y_i \log \hat{y}_i")],
    })
    elements = scan_special_elements(doc2)
    assert "fig1" in elements.figures
    assert "eq1" in elements.equations


def test_scan_variable_definitions():
    doc = make_doc(sections=[
        make_section("s1", "Method", "Let α denotes learning rate, and β represents momentum.")
    ])
    elements = scan_special_elements(doc)
    assert "α" in elements.variable_definitions


# ── chunker ───────────────────────────────────────────────────────────────────

def _structured_doc():
    return make_doc(sections=[
        make_section("s0", "Abstract", "We propose a novel approach. It outperforms all baselines."),
        make_section("s1", "Introduction", "Background information.\n\nMore context here."),
        make_section("s2", "Method", "Our method uses attention.\n\nSpecifically, we apply $W_q$ to queries."),
        make_section("s3", "Experiments", "We evaluate on GLUE.\n\nResults show F1=92."),
        make_section("s4", "Conclusion", "We proposed a framework.\n\nFuture work remains."),
    ])


def test_chunk_produces_abstract_chunk():
    doc = _structured_doc()
    chunks = CHUNKER.chunk(doc, "pymupdf")
    abstract_chunks = [c for c in chunks if c.chunk_type == "abstract"]
    assert len(abstract_chunks) == 1
    assert "novel approach" in abstract_chunks[0].content


def test_chunk_produces_parent_and_child():
    doc = _structured_doc()
    chunks = CHUNKER.chunk(doc, "pymupdf")
    parents = [c for c in chunks if c.metadata.get("is_parent")]
    children = [c for c in chunks if c.parent_chunk_id is not None]
    assert parents, "expected section-level parent chunks"
    assert children, "expected paragraph-level child chunks"


def test_child_references_parent():
    doc = _structured_doc()
    chunks = CHUNKER.chunk(doc, "pymupdf")
    parent_ids = {c.chunk_id for c in chunks if c.metadata.get("is_parent")}
    for child in [c for c in chunks if c.parent_chunk_id]:
        assert child.parent_chunk_id in parent_ids


def test_section_role_assigned():
    doc = _structured_doc()
    chunks = CHUNKER.chunk(doc, "pymupdf")
    method_chunks = [c for c in chunks if "method" in c.section_role]
    assert method_chunks


def test_formula_detected_in_paragraph():
    doc = _structured_doc()
    chunks = CHUNKER.chunk(doc, "latex")
    formula_chunks = [c for c in chunks if c.has_formula]
    assert formula_chunks, "expected at least one chunk with formula detected"


def test_formula_chunk_produced_with_context_and_parent():
    eq = make_equation("eq1", "$W_q$").model_copy(update={
        "page": 3,
        "metadata": {
            "source_locator": "paper://paper-1/pdf#page=3&pdf_rect=1,2,3,4",
            "pdf_rect": [1, 2, 3, 4],
        },
    })
    doc = _structured_doc().model_copy(update={"equations": [eq]})

    chunks = CHUNKER.chunk(doc, "latex")

    formula_chunks = [c for c in chunks if c.chunk_type == "formula"]
    assert len(formula_chunks) == 1
    formula = formula_chunks[0]
    assert formula.has_formula
    assert formula.formula_latex == "$W_q$"
    assert "LaTeX:" in formula.content
    assert "$W_q$" in formula.content
    assert "Context:" in formula.content
    assert "Specifically, we apply $W_q$ to queries." in formula.content
    assert formula.metadata["equation_id"] == "eq1"
    assert formula.metadata["page"] == 3
    assert formula.metadata["source_locator"] == "paper://paper-1/pdf#page=3&pdf_rect=1,2,3,4"
    assert formula.metadata["formula_parent_match_strategy"] == "latex_text"
    assert "eq1" in formula.metadata["reference_labels"]
    assert "1" in formula.metadata["reference_labels"]
    assert formula.metadata["referenced_by_chunks"] == []

    parent = next(c for c in chunks if c.chunk_id == formula.parent_chunk_id)
    assert parent.chunk_type == "paragraph"
    assert not parent.metadata.get("is_parent")
    assert parent.has_formula


def test_formula_chunk_records_multiple_explicit_references():
    eq = make_equation("eq_loss", r"\[L=x \tag{1}\]").model_copy(update={
        "page": 2,
        "metadata": {
            "source_locator": "paper://paper-1/pdf#page=2&pdf_rect=1,2,3,4",
            "equation_number": "1",
            "equation_label": "1",
        },
    })
    method = make_section(
        "s1",
        "Method",
        r"The objective is \[L=x \tag{1}\] for training.",
    ).model_copy(update={"metadata": {"source_locator": "paper://paper-1/pdf#page=2"}})
    analysis = make_section(
        "s2",
        "Analysis",
        "The loss behavior follows Eq. (1) under noisy labels.",
    ).model_copy(update={"metadata": {"source_locator": "paper://paper-1/pdf#page=3"}})
    experiments = make_section(
        "s3",
        "Experiments",
        "Equation 1 is reused in the ablation study.",
    ).model_copy(update={"metadata": {"source_locator": "paper://paper-1/pdf#page=4"}})
    doc = make_doc(sections=[
        make_section("s0", "Abstract", "Abstract."),
        method,
        analysis,
        experiments,
    ]).model_copy(update={"equations": [eq]})

    chunks = CHUNKER.chunk(doc, "pymupdf")

    formula = next(c for c in chunks if c.chunk_type == "formula")
    method_child = next(c for c in chunks if c.section_title == "Method" and not c.metadata.get("is_parent"))
    analysis_ref = next(c for c in chunks if c.section_title == "Analysis" and not c.metadata.get("is_parent"))
    experiments_ref = next(c for c in chunks if c.section_title == "Experiments" and not c.metadata.get("is_parent"))

    assert formula.parent_chunk_id == method_child.chunk_id
    assert formula.metadata["source_locator"] == "paper://paper-1/pdf#page=2&pdf_rect=1,2,3,4"
    assert formula.metadata["formula_parent_match_strategy"] == "latex_text"
    assert "1" in formula.metadata["reference_labels"]
    assert "loss" in formula.metadata["reference_labels"]
    assert formula.metadata["formula_normalized_latex"] == "l=x\\tag{1}"
    assert formula.metadata["formula_symbols"] == ["L", "x"]
    assert set(formula.metadata["formula_operators"]) >= {"=", "tag"}
    assert "loss behavior follows Eq. (1)" in formula.metadata["formula_referenced_text"][0]
    assert "Referenced By:" in formula.content
    assert "Equation 1 is reused in the ablation study." in formula.content
    assert formula.metadata["referenced_by_chunks"] == [
        {
            "chunk_id": analysis_ref.chunk_id,
            "section_title": "Analysis",
            "page": 3,
            "source_locator": "paper://paper-1/pdf#page=3",
            "text_ref": "Eq. (1)",
            "text": "The loss behavior follows Eq. (1) under noisy labels.",
        },
        {
            "chunk_id": experiments_ref.chunk_id,
            "section_title": "Experiments",
            "page": 4,
            "source_locator": "paper://paper-1/pdf#page=4",
            "text_ref": "Equation 1",
            "text": "Equation 1 is reused in the ablation study.",
        },
    ]
    assert analysis_ref.metadata["formula_references"] == [{
        "kind": "formula",
        "label": "1",
        "text_ref": "Eq. (1)",
        "equation_id": "eq_loss",
    }]
    assert experiments_ref.metadata["formula_references"] == [{
        "kind": "formula",
        "label": "1",
        "text_ref": "Equation 1",
        "equation_id": "eq_loss",
    }]


def test_unknown_formula_reference_is_not_linked():
    eq = make_equation("eq_loss", r"\[L=x \tag{1}\]").model_copy(update={
        "page": 2,
        "metadata": {
            "source_locator": "paper://paper-1/pdf#page=2&pdf_rect=1,2,3,4",
            "equation_number": "1",
        },
    })
    analysis = make_section(
        "s2",
        "Analysis",
        "The loss behavior is unrelated to Eq. (99).",
    ).model_copy(update={"metadata": {"source_locator": "paper://paper-1/pdf#page=3"}})
    doc = make_doc(sections=[
        make_section("s0", "Abstract", "Abstract."),
        make_section("s1", "Method", r"The objective is \[L=x \tag{1}\]."),
        analysis,
        make_section("s3", "Experiments", "Experiment text."),
    ]).model_copy(update={"equations": [eq]})

    chunks = CHUNKER.chunk(doc, "pymupdf")

    formula = next(c for c in chunks if c.chunk_type == "formula")
    analysis_ref = next(c for c in chunks if c.section_title == "Analysis" and not c.metadata.get("is_parent"))
    assert formula.metadata["referenced_by_chunks"] == []
    assert analysis_ref.metadata["formula_references"] == []


def test_formula_chunk_survives_unstructured_fallback():
    eq = make_equation("eq1", "$E=mc^2$").model_copy(update={
        "page": 2,
        "metadata": {
            "source_locator": "paper://paper-1/pdf#page=2&pdf_rect=4,5,6,7",
            "pdf_rect": [4, 5, 6, 7],
        },
    })
    doc = make_doc(sections=[
        make_section("s0", "Body", "The derivation depends on $E=mc^2$ in this paragraph."),
        make_section("s1", "More", "Additional unstructured text."),
    ]).model_copy(update={"equations": [eq]})

    chunks = CHUNKER.chunk(doc, "pymupdf")

    formula = next(c for c in chunks if c.chunk_type == "formula")
    assert formula.structure_detected is False
    assert formula.parent_chunk_id
    assert formula.metadata["formula_parent_match_strategy"] == "latex_text"
    assert formula.metadata["source_locator"] == "paper://paper-1/pdf#page=2&pdf_rect=4,5,6,7"


def test_overlap_applied():
    text = "First sentence ends here.\n\nSecond paragraph starts here and continues on."
    doc = make_doc(sections=[
        make_section("s0", "Abstract", "Abstract text."),
        make_section("s1", "Introduction", text),
        make_section("s2", "Method", "Method A.\n\nMethod B."),
        make_section("s3", "Experiments", "Exp A.\n\nExp B."),
    ])
    chunks = CHUNKER.chunk(doc, "pymupdf")
    second_child = [
        c for c in chunks
        if not c.metadata.get("is_parent") and c.metadata.get("para_index") == 1
        and "Introduction" in c.section_title
    ]
    first_child = [
        c for c in chunks
        if not c.metadata.get("is_parent") and c.metadata.get("para_index") == 0
        and "Introduction" in c.section_title
    ]
    assert second_child
    assert first_child
    # second paragraph should contain trailing sentence from first as overlap
    second = second_child[0]
    first = first_child[0]
    assert "First sentence ends here" in second.content
    assert second.metadata["content_span_unit"] == "char_offset"
    assert second.metadata["overlap_spans"] == [{
        "start": 0,
        "end": len("First sentence ends here."),
        "origin_chunk_id": first.chunk_id,
        "origin_source_locator": first.metadata["source_locator"],
        "overlap_type": "previous_paragraph_trailing_sentence",
    }]
    assert second.metadata["main_span"]["start"] == len("First sentence ends here.") + 1
    assert second.metadata["main_span"]["end"] == len(second.content)


def test_figure_caption_embedded():
    doc = make_doc(sections=[
        make_section("s0", "Abstract", "Abstract."),
        make_section("s1", "Introduction", "Intro."),
        make_section("s2", "Method", "As shown in Fig. 1, performance improves.\n\nMore details."),
        make_section("s3", "Experiments", "Experiment text."),
    ])
    doc2 = doc.model_copy(update={
        "figures": [make_figure("fig_1", "Performance improvement over baselines.")],
    })
    chunks = CHUNKER.chunk(doc2, "pymupdf")
    fig_ref_chunks = [c for c in chunks if c.has_figure]
    assert fig_ref_chunks


def test_figure_chunk_includes_context_and_trace_metadata():
    fig = make_figure("fig_1", "Performance improvement over baselines.").model_copy(update={
        "image_ref": "figures/fig1.png",
        "page": 3,
        "metadata": {
            "source_locator": "paper://paper-1/pdf#page=3&pdf_rect=10,20,30,40",
            "caption_source_locator": "paper://paper-1/pdf#page=3&pdf_rect=10,42,30,50",
            "pdf_rect": [10, 20, 30, 40],
            "caption_pdf_rect": [10, 42, 30, 50],
            "caption_text": "Figure 1: Performance improvement over baselines.",
            "ocr_text": "Encoder Decoder Attention",
            "ocr_text_source": "surya_ocr_crop",
            "ocr_attempted": True,
            "ocr_chars": 25,
        },
    })
    doc = make_doc(sections=[
        make_section("s0", "Abstract", "Abstract."),
        make_section("s1", "Introduction", "Intro."),
        make_section("s2", "Method", "As shown in Fig. 1, performance improves.\n\nMore details."),
        make_section("s3", "Experiments", "Experiment text."),
    ]).model_copy(update={"figures": [fig]})

    chunks = CHUNKER.chunk(doc, "pymupdf")

    figure = next(c for c in chunks if c.chunk_type == "figure")
    assert "[Figure fig_1]" in figure.content
    assert "Caption:" in figure.content
    assert "Nearby Context:" in figure.content
    assert "Section: Method" in figure.content
    assert "OCR Text:" in figure.content
    assert "Encoder Decoder Attention" in figure.content
    assert "Source: paper://paper-1/pdf#page=3&pdf_rect=10,20,30,40" in figure.content
    assert figure.parent_chunk_id
    assert figure.metadata["image_ref"] == "figures/fig1.png"
    assert figure.metadata["page"] == 3
    assert figure.metadata["pdf_rect"] == [10, 20, 30, 40]
    assert figure.metadata["caption_pdf_rect"] == [10, 42, 30, 50]
    assert figure.metadata["figure_parent_match_strategy"] == "caption_text"
    assert figure.metadata["visual_region"]["source_locator"] == "paper://paper-1/pdf#page=3&pdf_rect=10,20,30,40"
    assert figure.metadata["visual_region"]["pdf_rect"] == [10, 20, 30, 40]
    assert figure.metadata["caption_alignment"]["caption_text"] == "Figure 1: Performance improvement over baselines."
    assert figure.metadata["caption_alignment"]["caption_region"]["source_locator"] == (
        "paper://paper-1/pdf#page=3&pdf_rect=10,42,30,50"
    )
    assert figure.metadata["nearby_context_chunk_id"] == figure.parent_chunk_id
    assert "nearby_context" in figure.metadata["content_sources"]
    assert "ocr" in figure.metadata["content_sources"]


def test_cross_page_figure_reference_does_not_replace_visual_locator():
    intro = make_section(
        "s1",
        "Introduction",
        "The architecture is summarized in Figure 1 before the image appears.",
    ).model_copy(update={"metadata": {"source_locator": "paper://paper-1/pdf#page=1"}})
    method = make_section(
        "s2",
        "Method",
        "The caption is on the visual page.\n\nMore method details.",
    ).model_copy(update={"metadata": {"source_locator": "paper://paper-1/pdf#page=2"}})
    fig = make_figure("fig_1", "U-Net architecture.").model_copy(update={
        "image_ref": "figures/fig1.png",
        "page": 2,
        "metadata": {
            "source_locator": "paper://paper-1/pdf#page=2&pdf_rect=10,20,30,40",
            "caption_source_locator": "paper://paper-1/pdf#page=2&pdf_rect=10,42,30,50",
            "pdf_rect": [10, 20, 30, 40],
            "caption_pdf_rect": [10, 42, 30, 50],
            "caption_text": "Figure 1: U-Net architecture.",
            "alignment_strategy": "caption_region_number_match",
            "alignment_score": 0.98,
        },
    })
    doc = make_doc(sections=[
        make_section("s0", "Abstract", "Abstract."),
        intro,
        method,
        make_section("s3", "Experiments", "Experiment text."),
    ]).model_copy(update={"figures": [fig]})

    chunks = CHUNKER.chunk(doc, "pymupdf")

    figure = next(c for c in chunks if c.chunk_type == "figure")
    intro_ref = next(c for c in chunks if c.section_title == "Introduction" and not c.metadata.get("is_parent"))
    assert figure.metadata["source_locator"] == "paper://paper-1/pdf#page=2&pdf_rect=10,20,30,40"
    assert figure.metadata["visual_region"]["page"] == 2
    assert figure.metadata["caption_alignment"]["caption_match_strategy"] == "caption_region_number_match"
    assert figure.metadata["caption_alignment"]["caption_match_confidence"] == 0.98
    assert figure.metadata["referenced_by_chunks"] == [{
        "chunk_id": intro_ref.chunk_id,
        "section_title": "Introduction",
        "page": 1,
        "source_locator": "paper://paper-1/pdf#page=1",
        "text_ref": "Figure 1",
    }]
    assert intro_ref.metadata["visual_references"] == [{
        "kind": "figure",
        "label": "1",
        "text_ref": "Figure 1",
        "element_id": "fig_1",
    }]


def test_duplicate_figure_ids_use_visual_identity_for_chunk_ids():
    visual = make_figure("fig_split", "Ablation heatmap.").model_copy(update={
        "page": 35,
        "metadata": {
            "source_locator": "paper://paper-1/pdf#page=35&pdf_rect=63,65,565,190",
            "image_ref": "figures/surya_p035_fig011.png",
            "pdf_rect": [63, 65, 565, 190],
        },
    })
    caption_only = make_figure("fig_split", "Ablation heatmap from appendix.").model_copy(update={
        "page": 35,
        "metadata": {
            "source_locator": "paper://paper-1/pdf",
        },
    })
    doc = _structured_doc().model_copy(update={"figures": [visual, caption_only]})

    chunks = CHUNKER.chunk(doc, "pymupdf")

    figure_chunks = [
        c for c in chunks
        if c.chunk_type == "figure" and c.figure_id == "fig_split"
    ]
    assert len(figure_chunks) == 2
    assert len({c.chunk_id for c in figure_chunks}) == 2
    assert len({c.metadata["source_locator"] for c in figure_chunks}) == 2
    assert any(c.metadata["image_ref"] == "figures/surya_p035_fig011.png" for c in figure_chunks)
    assert all(c.metadata.get("figure_chunk_identity") for c in figure_chunks)


def test_table_chunk_produced():
    doc = make_doc(sections=[
        make_section("s0", "Abstract", "Abstract."),
        make_section("s1", "Introduction", "Intro."),
        make_section("s2", "Experiments", "Results in table 1."),
        make_section("s3", "Conclusion", "Conclusion."),
    ])
    doc2 = doc.model_copy(update={
        "tables": [make_table("tab1", "Main results", ["Model", "F1"])],
    })
    chunks = CHUNKER.chunk(doc2, "pymupdf")
    table_chunks = [c for c in chunks if c.chunk_type == "table"]
    assert table_chunks
    assert "tab1" in table_chunks[0].content


def test_table_chunk_renders_missing_slots_as_empty_without_literal_none():
    table = make_table("tab-missing", "Ragged", ["Metric", "Value"]).model_copy(update={
        "rows": [{"Metric": "recall", "Value": None}],
    })
    doc = make_doc(sections=[make_section("s1", "Results", "See the table.")]).model_copy(
        update={"tables": [table]},
    )
    chunk = next(item for item in CHUNKER.chunk(doc, "marker") if item.chunk_type == "table")
    assert "recall | " in chunk.content
    assert "None" not in chunk.content


def test_table_chunk_carries_cell_level_source_mapping():
    table_id = "tab-grid"
    locator = "paper://paper-1/pdf#page=4&table=1"
    grid = TableGrid(
        table_id=table_id,
        block_id="marker-table-1",
        row_count=2,
        column_count=2,
        has_header=False,
        source_locator=locator,
        cells=(
            TableCell(cell_id="h-a", row_index=0, column_index=0, status="content", text="Metric",
                      source_locator=f"{locator}&cell=0,0", bbox_unknown_reason="not provided"),
            TableCell(cell_id="h-b", row_index=0, column_index=1, status="content", text="Value",
                      source_locator=f"{locator}&cell=0,1", bbox_unknown_reason="not provided"),
            TableCell(cell_id="v-a", row_index=1, column_index=0, status="content", text="recall",
                      source_locator=f"{locator}&cell=1,0", bbox_unknown_reason="not provided"),
            TableCell(cell_id="v-b", row_index=1, column_index=1, status="blank",
                      source_locator=f"{locator}&cell=1,1", bbox_unknown_reason="not provided"),
        ),
    )
    table = make_table(table_id, "Results", ["Metric", "Value"]).model_copy(update={
        "rows": [{"Metric": "recall", "Value": ""}],
        "grid": grid,
        "metadata": {"source_locator": locator},
    })
    doc = make_doc(sections=[make_section("s1", "Results", "The table lists recall.")]).model_copy(
        update={"tables": [table]},
    )

    chunk = next(item for item in CHUNKER.chunk(doc, "marker") if item.chunk_type == "table")
    mapping = chunk.metadata["table_grid_source_map"]
    assert mapping["cell_mapping_status"] == "complete"
    assert mapping["source_grid_body_rows"] == [1]
    assert [item["cell_id"] for item in mapping["projection_header_cells"]] == ["h-a", "h-b"]
    assert [item["cell_id"] for item in mapping["projected_body_cells"]] == ["v-a", "v-b"]
    assert mapping["projected_body_cells"][1]["status"] == "blank"


def test_table_visual_description_remains_separate_from_cell_content_and_grid_truth():
    table_id = "tab-visual-separation"
    locator = "paper://paper-1/pdf#page=4&table=1"
    grid = TableGrid(
        table_id=table_id, block_id="marker-table-visual", row_count=2, column_count=1,
        has_header=False, source_locator=locator, cells=(
            TableCell(cell_id="metric-cell", row_index=0, column_index=0, status="content",
                      text="Metric", source_locator=f"{locator}&cell=0,0",
                      bbox_unknown_reason="not provided"),
            TableCell(cell_id="value-cell", row_index=1, column_index=0, status="content",
                      text="accuracy 0.91", source_locator=f"{locator}&cell=1,0",
                      bbox_unknown_reason="not provided"),
        ),
    )
    table = make_table(table_id, "Results", ["Metric"]).model_copy(update={
        "rows": [{"Metric": "accuracy 0.91"}], "grid": grid,
        "metadata": {"source_locator": locator,
                     "visual_description": "A red arrow suggests the value is 0.99."},
    })
    document = make_doc(sections=[make_section("results", "Results", "Table results.")]).model_copy(
        update={"tables": [table]},
    )

    chunk = next(item for item in CHUNKER.chunk(document, "marker") if item.chunk_type == "table")

    assert "accuracy 0.91" in chunk.content
    assert "0.99" not in chunk.content
    assert chunk.metadata["visual_description"] == "A red arrow suggests the value is 0.99."
    source_map = chunk.metadata["table_grid_source_map"]
    assert [(cell["cell_id"], cell["status"], cell["source_locator"])
            for cell in source_map["projection_header_cells"]] == [
        ("metric-cell", "content", f"{locator}&cell=0,0"),
    ]
    assert [(cell["cell_id"], cell["status"], cell["source_locator"])
            for cell in source_map["projected_body_cells"]] == [
        ("value-cell", "content", f"{locator}&cell=1,0"),
    ]


def test_table_chunk_includes_context_rows_and_trace_metadata():
    section = make_section(
        "s2",
        "Experiments",
        "The ablation numbers are summarized on this page.",
    ).model_copy(update={
        "metadata": {"source_locator": "paper://paper-1/pdf#page=6"},
    })
    table = make_table("tab1", "Main ablation results", ["Model", "F1"]).model_copy(update={
        "page": 6,
        "rows": [{"Model": "base", "F1": "91.0"}, {"Model": "large", "F1": "93.2"}],
        "metadata": {
            "source_locator": "paper://paper-1/pdf#page=6&pdf_rect=50,60,300,200",
            "image_ref": "tables/tab1.png",
            "pdf_rect": [50, 60, 300, 200],
            "caption_pdf_rect": [50, 40, 300, 55],
        },
    })
    doc = make_doc(sections=[
        make_section("s0", "Abstract", "Abstract."),
        make_section("s1", "Introduction", "Intro."),
        section,
        make_section("s3", "Conclusion", "Conclusion."),
    ]).model_copy(update={"tables": [table]})

    chunks = CHUNKER.chunk(doc, "pymupdf")

    table_chunk = next(c for c in chunks if c.chunk_type == "table")
    assert "[Table tab1]" in table_chunk.content
    assert "Caption:" in table_chunk.content
    assert "Columns:" in table_chunk.content
    assert "Rows:" in table_chunk.content
    assert "base | 91.0" in table_chunk.content
    assert "Nearby Context:" in table_chunk.content
    assert table_chunk.parent_chunk_id
    assert table_chunk.metadata["table_id"] == "tab1"
    assert table_chunk.metadata["page"] == 6
    assert table_chunk.metadata["image_ref"] == "tables/tab1.png"
    assert table_chunk.metadata["table_parent_match_strategy"] == "page_nearest"
    assert table_chunk.metadata["visual_region"]["source_locator"] == (
        "paper://paper-1/pdf#page=6&pdf_rect=50,60,300,200"
    )
    assert table_chunk.metadata["caption_alignment"]["caption_region"]["pdf_rect"] == [50, 40, 300, 55]
    assert table_chunk.metadata["nearby_context_chunk_id"] == table_chunk.parent_chunk_id
    assert "rows" in table_chunk.metadata["content_sources"]


def test_table_chunk_records_explicit_body_references():
    intro = make_section(
        "s1",
        "Introduction",
        "The main results are reported in Table 1 before the table appears.",
    ).model_copy(update={"metadata": {"source_locator": "paper://paper-1/pdf#page=2"}})
    table_section = make_section(
        "s2",
        "Experiments",
        "The table is rendered on this page.",
    ).model_copy(update={"metadata": {"source_locator": "paper://paper-1/pdf#page=6"}})
    table = make_table("tab1", "Main ablation results", ["Model", "F1"]).model_copy(update={
        "page": 6,
        "rows": [{"Model": "base", "F1": "91.0"}],
        "metadata": {
            "source_locator": "paper://paper-1/pdf#page=6&pdf_rect=50,60,300,200",
            "caption_source_locator": "paper://paper-1/pdf#page=6&pdf_rect=50,40,300,55",
            "caption_text": "Table 1: Main ablation results.",
            "pdf_rect": [50, 60, 300, 200],
            "caption_pdf_rect": [50, 40, 300, 55],
            "alignment_strategy": "caption_region_number_match",
            "alignment_score": 0.92,
        },
    })
    doc = make_doc(sections=[
        make_section("s0", "Abstract", "Abstract."),
        intro,
        table_section,
        make_section("s3", "Conclusion", "Conclusion."),
    ]).model_copy(update={"tables": [table]})

    chunks = CHUNKER.chunk(doc, "pymupdf")

    table_chunk = next(c for c in chunks if c.chunk_type == "table")
    intro_ref = next(c for c in chunks if c.section_title == "Introduction" and not c.metadata.get("is_parent"))
    assert table_chunk.metadata["source_locator"] == "paper://paper-1/pdf#page=6&pdf_rect=50,60,300,200"
    assert table_chunk.metadata["caption_alignment"]["caption_match_strategy"] == "caption_region_number_match"
    assert table_chunk.metadata["caption_alignment"]["caption_match_confidence"] == 0.92
    assert table_chunk.metadata["referenced_by_chunks"] == [{
        "chunk_id": intro_ref.chunk_id,
        "section_title": "Introduction",
        "page": 2,
        "source_locator": "paper://paper-1/pdf#page=2",
        "text_ref": "Table 1",
    }]


def test_long_table_emits_row_group_chunks():
    rows = [{"Model": f"model-{index}", "F1": str(80 + index)} for index in range(45)]
    table = make_table("tab-long", "Long benchmark results", ["Model", "F1"]).model_copy(update={
        "rows": rows,
        "metadata": {"source_locator": "paper://paper-1/pdf#page=7"},
    })
    doc = _structured_doc().model_copy(update={"tables": [table]})

    chunks = CHUNKER.chunk(doc, "pymupdf")

    table_chunks = [
        c for c in chunks
        if c.chunk_type == "table" and c.metadata.get("table_id") == "tab-long"
    ]
    row_groups = [c for c in table_chunks if c.metadata.get("is_table_row_group")]
    parent = next(c for c in table_chunks if not c.metadata.get("is_table_row_group"))
    assert len(row_groups) == 3
    assert [(c.metadata["row_start"], c.metadata["row_end"]) for c in row_groups] == [
        (0, 19),
        (20, 39),
        (40, 44),
    ]
    assert all(c.parent_chunk_id == parent.chunk_id for c in row_groups)
    assert "model-44 | 124" in row_groups[-1].content
    assert "model-44 | 124" not in parent.content


def test_duplicate_table_ids_use_visual_identity_for_chunk_ids():
    left = make_table("tab-split", "Split ablation table", ["Model", "Score"]).model_copy(update={
        "page": 16,
        "rows": [{"Model": "Hyena", "Score": "10.24"}],
        "metadata": {
            "source_locator": "paper://paper-1/pdf#page=16&pdf_rect=101,106,308,193",
            "image_ref": "tables/surya_table_p016_006.png",
            "pdf_rect": [101, 106, 308, 193],
        },
    })
    right = make_table("tab-split", "Split ablation table", ["Model", "Score"]).model_copy(update={
        "page": 16,
        "rows": [{"Model": "Mamba", "Score": "10.75"}],
        "metadata": {
            "source_locator": "paper://paper-1/pdf#page=16&pdf_rect=310,106,527,193",
            "image_ref": "tables/surya_table_p016_007.png",
            "pdf_rect": [310, 106, 527, 193],
        },
    })
    doc = _structured_doc().model_copy(update={"tables": [left, right]})

    chunks = CHUNKER.chunk(doc, "pymupdf")

    table_chunks = [
        c for c in chunks
        if c.chunk_type == "table" and c.metadata.get("table_id") == "tab-split"
    ]
    assert len(table_chunks) == 2
    assert len({c.chunk_id for c in table_chunks}) == 2
    assert len({c.metadata["source_locator"] for c in table_chunks}) == 2
    assert len({c.metadata["image_ref"] for c in table_chunks}) == 2
    assert all(c.metadata.get("table_chunk_identity") for c in table_chunks)


def test_fallback_for_unstructured_doc():
    # Only 2 non-abstract sections → triggers fallback
    doc = make_doc(sections=[
        make_section("s0", "Abstract", "Abstract text."),
        make_section("s1", "Introduction", "Intro text."),
        make_section("s2", "Method", "Method text."),
    ])
    chunks = CHUNKER.chunk(doc, "pymupdf")
    assert all(not c.structure_detected for c in chunks)


def test_structure_detected_flag():
    doc = _structured_doc()
    chunks = CHUNKER.chunk(doc, "pymupdf")
    assert all(c.structure_detected for c in chunks)


def test_parse_source_propagated():
    doc = _structured_doc()
    for source in ("latex", "marker", "pymupdf", "nougat"):
        chunks = CHUNKER.chunk(doc, source)  # type: ignore[arg-type]
        assert all(c.parse_source == source for c in chunks)


def test_chunk_model_required_fields():
    with pytest.raises(Exception):
        PaperChunk(chunk_id="", paper_id="p1", parse_source="latex", content="text")

    with pytest.raises(Exception):
        PaperChunk(chunk_id="c1", paper_id="p1", parse_source="latex", content="")


# ── routing ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("question,expected_intent", [
    ("图3说明了什么？", "figure_query"),
    ("What does Figure 3 show?", "figure_query"),
    ("What does Table 2 report?", "table_query"),
    ("这个公式的含义是什么？", "formula_query"),
    ("在GLUE基准上F1达到了多少？", "numerical_result"),
    ("这篇论文的主要贡献是什么？", "contribution"),
    ("与BERT相比性能如何？", "comparison"),
    ("作者的方法使用了什么架构？", "concept_method"),
])
def test_classify_query_intent(question, expected_intent):
    assert classify_query_intent(question) == expected_intent


def test_build_retrieval_route_figure():
    route = build_retrieval_route("图3展示了什么？")
    assert route.intent == "figure_query"
    assert "figure" in route.chunk_type_filter


def test_build_retrieval_route_table():
    route = build_retrieval_route("What does Table 2 report?")
    assert route.intent == "table_query"
    assert "table" in route.chunk_type_filter
    assert "paragraph" in route.chunk_type_filter
    assert route.extra_filters == {}
    assert route.candidate_filter_groups == (
        {"chunk_type": "table"},
        {"chunk_type": "paragraph"},
    )


def test_build_retrieval_route_method():
    route = build_retrieval_route("作者如何设计模型架构？")
    assert route.intent == "concept_method"
    assert "method" in route.section_role_filter


def test_build_retrieval_route_experiment():
    route = build_retrieval_route("在GLUE上准确率是多少？")
    assert route.intent == "numerical_result"
    assert route.use_propositions


# ── edge cases ─────────────────────────────────────────────────────────────────

def test_empty_document_produces_no_chunks():
    doc = make_doc(sections=[])
    chunks = CHUNKER.chunk(doc, "latex")
    assert chunks == []


def test_sections_with_blank_text_handled():
    doc = make_doc(sections=[
        make_section("s0", "Abstract", "   "),
        make_section("s1", "Introduction", ""),
        make_section("s2", "Method", "Real method content here."),
        make_section("s3", "Experiments", "Real experiment content."),
    ])
    # should not raise; only non-empty sections yield content chunks
    chunks = CHUNKER.chunk(doc, "latex")
    assert all(c.content.strip() for c in chunks)


def test_long_unstructured_doc_triggers_token_fallback():
    long_text = " ".join(f"word{i}" for i in range(4000))
    doc = make_doc(sections=[
        make_section("s0", "Body", long_text),
        make_section("s1", "More", long_text),
    ])
    chunks = CHUNKER.chunk(doc, "pymupdf")
    assert chunks
    assert all(not c.structure_detected for c in chunks)
    # fallback splits into multiple fixed-window chunks
    assert len(chunks) > 1


def test_cross_section_reference_recorded():
    doc = make_doc(sections=[
        make_section("s0", "Abstract", "Abstract."),
        make_section("s1", "Introduction", "Intro."),
        make_section("s2", "Method", "As shown in 公式(1), the loss is computed. 详见第3节。"),
        make_section("s3", "Experiments", "Experiment results."),
    ])
    chunks = CHUNKER.chunk(doc, "latex")
    method_chunks = [c for c in chunks if "Method" in c.section_title]
    # at least one method chunk should carry recorded cross-references
    assert any(c.references for c in method_chunks)


def test_chunk_ids_unique():
    doc = _structured_doc()
    chunks = CHUNKER.chunk(doc, "latex")
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))


def test_chunks_include_semantic_key_metadata():
    doc = _structured_doc()
    chunks = CHUNKER.chunk(doc, "latex")

    assert chunks
    assert all(c.metadata.get("semantic_key") for c in chunks)
    assert all(c.metadata.get("content_hash") for c in chunks)
    assert all(c.metadata.get("source_locator") for c in chunks)


def test_chunk_manifest_reuses_id_when_paragraph_index_shifts(tmp_path):
    first_doc = make_doc(sections=[
        make_section("s0", "Abstract", "Abstract."),
        make_section("s1", "Introduction", "Intro."),
        make_section(
            "s2",
            "Method",
            "First method paragraph.\n\nTarget paragraph should keep its identity.",
        ),
        make_section("s3", "Experiments", "Experiment results."),
    ])
    second_doc = make_doc(sections=[
        make_section("s0", "Abstract", "Abstract."),
        make_section("s1", "Introduction", "Intro."),
        make_section(
            "s2",
            "Method",
            "Inserted parser recovery paragraph.\n\n"
            "First method paragraph.\n\n"
            "Target paragraph should keep its identity.",
        ),
        make_section("s3", "Experiments", "Experiment results."),
    ])
    manager = ChunkManifestManager(tmp_path / "chunk_manifest.json")

    first_chunks = manager.resolve_chunk_ids(
        first_doc.paper_id,
        CHUNKER.chunk(first_doc, "latex"),
    )
    manager.write(first_doc.paper_id, first_chunks)
    first_target = _find_chunk(first_chunks, "Target paragraph should keep its identity.")
    first_source = _find_chunk(first_chunks, "First method paragraph.")

    second_generated = CHUNKER.chunk(second_doc, "latex")
    second_generated_target = _find_chunk(
        second_generated,
        "Target paragraph should keep its identity.",
    )
    second_generated_source = _find_chunk(second_generated, "First method paragraph.")
    assert second_generated_target.chunk_id != first_target.chunk_id
    assert second_generated_target.metadata["overlap_spans"][0]["origin_chunk_id"] == second_generated_source.chunk_id

    second_chunks = manager.resolve_chunk_ids(second_doc.paper_id, second_generated)
    second_target = _find_chunk(second_chunks, "Target paragraph should keep its identity.")

    assert second_target.chunk_id == first_target.chunk_id
    assert second_target.metadata["semantic_key"] == first_target.metadata["semantic_key"]
    assert second_target.metadata["overlap_spans"][0]["origin_chunk_id"] == first_source.chunk_id
    manifest = json.loads((tmp_path / "chunk_manifest.json").read_text(encoding="utf-8"))
    entries = {entry["semantic_key"]: entry for entry in manifest["chunks"]}
    assert entries[first_target.metadata["semantic_key"]]["chunk_id"] == first_target.chunk_id


def test_chunk_manifest_concurrent_same_paper_writes_converge_with_source_history(tmp_path):
    root = tmp_path / "owned-manifest"
    while len(os.fspath(root)) <= 270:
        root /= "long-manifest-storage-abcdefghijklmnopqrstuvwxyz"
    manifest_path = root / "chunk_manifest.json"
    manager = ChunkManifestManager(manifest_path)
    doc = make_doc(sections=[make_section("s0", "Methods", "Concurrent source content.")])
    chunks = CHUNKER.chunk(doc, "latex")
    scope = {"user_id": "alice", "memory_namespace": "research:user:alice"}
    writes = [
        {
            "document_id": "document-0",
            "source_hash": "sha256:" + "0" * 64,
            "run_id": "run-0",
            "observed_at": "2026-10-16T00:00:00+00:00",
        },
        {
            "document_id": "document-1",
            "source_hash": "sha256:" + "1" * 64,
            "run_id": "run-1",
            "observed_at": "2026-10-16T00:00:01+00:00",
        },
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda item: manager.write(
                    doc.paper_id,
                    chunks,
                    document_metadata={
                        "document_id": item["document_id"],
                        "source_hash": item["source_hash"],
                        "actor_scope": scope,
                    },
                    actor_scope=scope,
                    **item,
                ),
                writes,
            )
        )

    assert not os.fspath(manifest_path).startswith("\\\\?\\")
    persisted_path = manager.path_for(doc.paper_id, actor_scope=scope)
    payload = json.loads(storage_io_path(persisted_path).read_text(encoding="utf-8"))
    persisted_sources = {
        str(payload.get("source_hash")),
        *(str(entry.get("source_hash")) for entry in payload.get("history", [])),
    }
    assert persisted_sources == {item["source_hash"] for item in writes}
    assert payload["paper_id"] == doc.paper_id
    assert payload["actor_scope"] == scope
    assert payload["run_id"] in {item["run_id"] for item in writes}
    assert len(payload["history"]) == 1
    assert payload["chunks"]
    assert not list(storage_io_path(persisted_path.parent).glob(f".{persisted_path.name}.*.tmp"))


def test_chunk_manifest_includes_parser_cascade_metadata(tmp_path):
    manager = ChunkManifestManager(tmp_path / "chunk_manifest.json")
    doc = make_doc(sections=[make_section("s0", "Introduction", "Intro.")]).model_copy(update={
        "metadata": {
            "parser_cascade": {
                "used_backend": "marker",
                "degraded": False,
                "attempts": [{"backend": "marker", "status": "success"}],
            }
        }
    })
    chunks = manager.resolve_chunk_ids(doc.paper_id, CHUNKER.chunk(doc, "marker"))

    manager.write(doc.paper_id, chunks, document_metadata=doc.metadata)

    manifest = json.loads((tmp_path / "chunk_manifest.json").read_text(encoding="utf-8"))
    assert manifest["parser_cascade"] == doc.metadata["parser_cascade"]


def test_chunk_manifest_splits_duplicate_generated_chunk_ids(tmp_path):
    manager = ChunkManifestManager(tmp_path / "chunk_manifest.json")
    chunks = [
        PaperChunk(
            chunk_id="chunk_duplicate",
            paper_id="paper-1",
            parse_source="pymupdf",
            chunk_type="table",
            section_title="Experiments",
            has_table=True,
            content="left table rows",
            metadata={
                "source_ref": "paper://paper-1",
                "source_locator": "paper://paper-1/pdf#page=16&pdf_rect=101,106,308,193",
            },
        ),
        PaperChunk(
            chunk_id="chunk_duplicate",
            paper_id="paper-1",
            parse_source="pymupdf",
            chunk_type="table",
            section_title="Experiments",
            has_table=True,
            content="right table rows",
            metadata={
                "source_ref": "paper://paper-1",
                "source_locator": "paper://paper-1/pdf#page=16&pdf_rect=310,106,527,193",
            },
        ),
    ]

    resolved = manager.resolve_chunk_ids("paper-1", chunks)

    assert len({chunk.chunk_id for chunk in resolved}) == 2
    assert {chunk.metadata["source_locator"] for chunk in resolved} == {
        "paper://paper-1/pdf#page=16&pdf_rect=101,106,308,193",
        "paper://paper-1/pdf#page=16&pdf_rect=310,106,527,193",
    }


def _find_chunk(chunks: list[PaperChunk], text: str) -> PaperChunk:
    return next(
        chunk for chunk in chunks
        if not chunk.metadata.get("is_parent") and text in chunk.content
    )


# ── boilerplate section filtering ──────────────────────────────────────────────

def test_is_boilerplate_section():
    from backend.research.document.section_detector import is_boilerplate_section
    assert is_boilerplate_section("Acknowledgments")
    assert is_boilerplate_section("Acknowledgements")
    assert is_boilerplate_section("Funding")
    assert is_boilerplate_section("References")
    assert is_boilerplate_section("Appendix A")
    assert is_boilerplate_section("Broader Impact")
    assert is_boilerplate_section("致谢")
    assert is_boilerplate_section("参考文献")
    # content sections are NOT boilerplate
    assert not is_boilerplate_section("Method")
    assert not is_boilerplate_section("Experiments")
    assert not is_boilerplate_section("Introduction")


def test_boilerplate_content_and_meta_question_guards():
    from backend.research.rag.evaluation.paper_gold_builder import _is_boilerplate_content, _is_meta_question
    assert _is_boilerplate_content("This work was supported by grant no. 12345")
    assert _is_boilerplate_content("We thank the reviewers for their feedback")
    assert not _is_boilerplate_content("The model uses multi-head attention")
    assert _is_meta_question("Which section presents the BERT results?")
    assert _is_meta_question("What are the exact names after the funding statement?")
    assert not _is_meta_question("How does multi-head attention scale queries?")


def test_production_chunks_carry_bounded_fidelity_order_and_locator_maps():
    source_hash = "sha256-source"
    source_ref = "paper://paper-1/pdf"
    equation = make_equation("eq-fidelity", "x^2 + y^2 = z^2", source_ref).model_copy(update={
        "page": 1,
        "metadata": {
            "source_locator": f"{source_ref}#page=1",
            "marker_provenance": {"block_id": "equation-block"},
        },
    })
    section = make_section("fidelity-section", "Results", "The result follows from the equation.",
                           source_ref=f"{source_ref}#page=1").model_copy(update={
        "page_start": 1, "page_end": 1,
        "metadata": {
            "source_locator": f"{source_ref}#page=1",
            "marker_provenance": {"blocks": [
                {"block_id": "body-block"},
            ]},
        },
    })
    fidelity = DocumentFidelity(
        schema_version="research-document-fidelity.v1",
        normalization_version="normalizer/v1", source_hash=source_hash,
        parser_fingerprint="sha256:parser", profile_fingerprint="sha256:profile",
        content_checksum="sha256:raw",
        pages=(DocumentPage(pdf_page_number=1, width=612, height=792, dimension_unit="pt"),),
        blocks=(
            DocumentBlock(block_id="body-block", page_number=1, kind="text", source_order=0,
                          reading_order=0, order_status="preserved", source_locator=f"{source_ref}#page=1",
                          bbox_unknown_reason="geometry unavailable", text=section.text,
                          content_owner_id="body-block"),
            DocumentBlock(block_id="equation-block", page_number=1, kind="equation", source_order=1,
                          reading_order=1, order_status="preserved", element_ref=equation.equation_id,
                          source_locator=f"{source_ref}#page=1",
                          bbox_unknown_reason="geometry unavailable", text=equation.latex,
                          content_owner_id="equation-block"),
        ),
    )
    document = make_doc(sections=[
        make_section("intro", "Introduction", "An introduction."),
        section,
        make_section("conclusion", "Conclusion", "A conclusion."),
    ]).model_copy(update={
        "source_hash": source_hash, "equations": [equation], "fidelity": fidelity,
    })

    chunks = CHUNKER.chunk(document, "marker")
    section_parent = next(chunk for chunk in chunks if chunk.chunk_type == "paragraph" and chunk.metadata.get("section_id") == "fidelity-section")
    formula_chunk = next(chunk for chunk in chunks if chunk.chunk_type == "formula")

    section_map = section_parent.metadata["fidelity_source_map"]
    assert section_map["mapping_status"] == "complete"
    assert [item["block_id"] for item in section_map["blocks"]] == ["body-block"]
    assert [item["reading_order"] for item in section_map["blocks"]] == [0]
    formula_map = formula_chunk.metadata["fidelity_source_map"]
    assert formula_map["mapping_status"] == "complete"
    assert formula_map["blocks"][0]["element_ref"] == equation.equation_id
    assert formula_map["blocks"][0]["source_locator"].endswith("#page=1")
    serialized_formula = formula_chunk.model_dump(mode="json", exclude_none=True)
    assert serialized_formula["metadata"]["fidelity_source_map"] == formula_map

    unmapped_parent = next(
        chunk for chunk in chunks
        if chunk.chunk_type == "paragraph" and chunk.metadata.get("section_id") == "intro"
    )
    assert unmapped_parent.metadata["fidelity_source_map"]["mapping_status"] == "unavailable"

    mismatched_document = document.model_copy(update={"source_hash": "sha256-different-source"})
    mismatched_chunks = CHUNKER.chunk(mismatched_document, "marker")
    mismatched_parent = next(
        chunk for chunk in mismatched_chunks
        if chunk.chunk_type == "paragraph" and chunk.metadata.get("section_id") == "fidelity-section"
    )
    assert mismatched_parent.metadata["fidelity_source_map"]["mapping_status"] == "quarantined"
    assert mismatched_parent.metadata["fidelity_source_map"]["blocks"] == []


def test_paragraph_children_map_cross_page_duplicate_text_and_raw_inline_math_offsets():
    source_ref = "paper://paper-1/pdf"
    repeated = "Repeated paragraph."
    page_two_text = "Repeated paragraph.\n\nInline $x+y$ closes."
    inline_start = page_two_text.index("$x+y$")
    target_text = f"{repeated}\n\n{page_two_text}"
    target = make_section(
        "cross-page", "Results", target_text, source_ref=f"{source_ref}#page=1"
    ).model_copy(update={
        "page_start": 1,
        "page_end": 2,
        "metadata": {
            "source_locator": f"{source_ref}#page=1",
            "marker_provenance": {
                "blocks": [
                    {"block_id": "page-1-body", "raw_text": repeated},
                    {"block_id": "page-2-body", "raw_text": page_two_text},
                ],
            },
        },
    })
    fidelity = DocumentFidelity(
        schema_version="research-document-fidelity.v1",
        normalization_version="normalizer/v1",
        source_hash="sha256-test",
        parser_fingerprint="sha256:parser",
        profile_fingerprint="sha256:profile",
        content_checksum="sha256:raw",
        pages=(
            DocumentPage(pdf_page_number=1, width=612, height=792, dimension_unit="pt"),
            DocumentPage(pdf_page_number=2, width=612, height=792, dimension_unit="pt"),
        ),
        blocks=(
            DocumentBlock(
                block_id="page-1-body", page_number=1, kind="text", source_order=0,
                reading_order=0, order_status="preserved", text=repeated,
                source_locator=f"{source_ref}#page=1",
                bbox_unknown_reason="geometry unavailable", content_owner_id="page-1-body",
            ),
            DocumentBlock(
                block_id="page-2-body", page_number=2, kind="text", source_order=0,
                reading_order=0, order_status="preserved", text=page_two_text,
                source_locator=f"{source_ref}#page=2",
                bbox_unknown_reason="geometry unavailable", content_owner_id="page-2-body",
                inline_math=(InlineMathSpan(
                    start_offset=inline_start, end_offset=inline_start + len("$x+y$"),
                    offset_text_version="raw", raw_latex="$x+y$",
                    source_locator=f"{source_ref}#page=2",
                ),),
            ),
        ),
    )
    document = make_doc(sections=[
        make_section("intro", "Introduction", "Intro body."),
        target,
        make_section("conclusion", "Conclusion", "Conclusion body."),
    ]).model_copy(update={"fidelity": fidelity})

    chunks = CHUNKER.chunk(document, "marker")
    children = [
        chunk for chunk in chunks
        if chunk.chunk_type == "paragraph"
        and chunk.metadata.get("section_id") == "cross-page"
        and chunk.metadata.get("is_parent") is False
    ]

    assert len(children) == 3
    source_maps = [child.metadata["fidelity_source_map"] for child in children]
    assert [mapping["mapping_status"] for mapping in source_maps] == [
        "complete", "complete", "partial",
    ]
    assert [mapping["blocks"][0]["block_id"] for mapping in source_maps] == [
        "page-1-body", "page-2-body", "page-2-body",
    ]
    assert [mapping["blocks"][0]["page_number"] for mapping in source_maps] == [1, 2, 2]
    assert source_maps[1]["blocks"][0]["inline_math_spans"] == []
    inline_entry = source_maps[2]["blocks"][0]
    inline_paragraph_start = page_two_text.index("Inline")
    assert inline_entry["block_text_ranges"] == [{
        "start_offset": inline_paragraph_start,
        "end_offset": len(page_two_text),
        "offset_basis": "unicode_codepoint",
        "offset_text_version": "normalized",
    }]
    assert inline_entry["raw_math_offset_compatibility"] == "confirmed"
    assert inline_entry["inline_math_spans"] == [{
        "start_offset": inline_start,
        "end_offset": inline_start + len("$x+y$"),
        "offset_text_version": "raw",
        "display": False,
        "source_locator": f"{source_ref}#page=2",
    }]
    assert inline_entry["inline_math_spans"][0]["start_offset"] > 0
    assert source_maps[2]["mapping_scope"] == "paragraph_source_text"
    assert source_maps[2]["chunk_content_coverage"] == "partial"
    assert source_maps[2]["excluded_chunk_content"] == ["overlap_prefix"]

    persisted = json.loads(json.dumps(
        [child.model_dump(mode="json", exclude_none=True) for child in children]
    ))
    restored = [PaperChunk.model_validate(item) for item in persisted]
    assert restored[2].metadata["fidelity_source_map"] == source_maps[2]


def test_paragraph_child_does_not_apply_normalized_range_to_mismatched_raw_math_offsets():
    source_ref = "paper://paper-1/pdf"
    normalized_text = "Normalized π before $x$ after."
    raw_text = "Normalized pi before $x$ after."
    raw_start = raw_text.index("$x$")
    target = make_section(
        "raw-offset-mismatch", "Results", normalized_text, source_ref=f"{source_ref}#page=1"
    ).model_copy(update={
        "metadata": {
            "source_locator": f"{source_ref}#page=1",
            "marker_provenance": {
                "blocks": [{"block_id": "normalized-body", "raw_text": raw_text}],
            },
        },
    })
    fidelity = DocumentFidelity(
        schema_version="research-document-fidelity.v1",
        normalization_version="normalizer/v1",
        source_hash="sha256-test",
        parser_fingerprint="sha256:parser",
        profile_fingerprint="sha256:profile",
        content_checksum="sha256:raw",
        pages=(DocumentPage(pdf_page_number=1, width=612, height=792, dimension_unit="pt"),),
        blocks=(DocumentBlock(
            block_id="normalized-body", page_number=1, kind="text", source_order=0,
            reading_order=0, order_status="preserved", text=normalized_text,
            source_locator=f"{source_ref}#page=1",
            bbox_unknown_reason="geometry unavailable", content_owner_id="normalized-body",
            inline_math=(InlineMathSpan(
                start_offset=raw_start, end_offset=raw_start + len("$x$"),
                offset_text_version="raw", raw_latex="$x$",
                source_locator=f"{source_ref}#page=1",
            ),),
        ),),
    )
    document = make_doc(sections=[
        make_section("intro", "Introduction", "Intro body."),
        target,
        make_section("conclusion", "Conclusion", "Conclusion body."),
    ]).model_copy(update={"fidelity": fidelity})

    child = next(
        chunk for chunk in CHUNKER.chunk(document, "marker")
        if chunk.chunk_type == "paragraph"
        and chunk.metadata.get("section_id") == "raw-offset-mismatch"
        and chunk.metadata.get("is_parent") is False
    )

    source_map = child.metadata["fidelity_source_map"]
    block_map = source_map["blocks"][0]
    assert source_map["mapping_status"] == "partial"
    assert source_map["mapping_scope"] == "paragraph_source_text"
    assert source_map["chunk_content_coverage"] == "complete"
    assert source_map["omitted_inline_math_span_count"] == 1
    assert block_map["block_text_ranges"] == [{
        "start_offset": 0,
        "end_offset": len(normalized_text),
        "offset_basis": "unicode_codepoint",
        "offset_text_version": "normalized",
    }]
    assert block_map["inline_math_candidate_span_count"] == 1
    assert block_map["raw_math_offset_compatibility"] == "mismatch"
    assert block_map["inline_math_span_count"] == 0
    assert block_map["unmapped_inline_math_span_count"] == 1
    assert block_map["budget_omitted_inline_math_span_count"] == 0
    assert block_map["omitted_inline_math_span_count"] == 1
    assert block_map["inline_math_spans"] == []


def test_paragraph_child_fidelity_map_is_unavailable_when_source_range_cannot_be_proven():
    target = make_section("unaligned", "Results", "Projected paragraph.").model_copy(update={
        "metadata": {
            "marker_provenance": {"blocks": [{"block_id": "different-source"}]},
        },
    })
    fidelity = DocumentFidelity(
        schema_version="research-document-fidelity.v1",
        normalization_version="normalizer/v1",
        source_hash="sha256-test",
        parser_fingerprint="sha256:parser",
        profile_fingerprint="sha256:profile",
        content_checksum="sha256:raw",
        pages=(DocumentPage(pdf_page_number=1, width=612, height=792, dimension_unit="pt"),),
        blocks=(DocumentBlock(
            block_id="different-source", page_number=1, kind="text", source_order=0,
            reading_order=0, order_status="preserved", text="Different source text.",
            source_locator="paper://paper-1#page=1",
            bbox_unknown_reason="geometry unavailable", content_owner_id="different-source",
        ),),
    )
    document = make_doc(sections=[
        make_section("intro", "Introduction", "Intro body."),
        target,
        make_section("conclusion", "Conclusion", "Conclusion body."),
    ]).model_copy(update={"fidelity": fidelity})

    child = next(
        chunk for chunk in CHUNKER.chunk(document, "marker")
        if chunk.chunk_type == "paragraph"
        and chunk.metadata.get("section_id") == "unaligned"
        and chunk.metadata.get("is_parent") is False
    )

    source_map = child.metadata["fidelity_source_map"]
    assert source_map["mapping_status"] == "unavailable"
    assert source_map["reason"] == "paragraph_source_range_unavailable"
    assert source_map["blocks"] == []


def test_fallback_paragraph_chunk_reports_unavailable_fidelity_mapping():
    fidelity = DocumentFidelity(
        schema_version="research-document-fidelity.v1",
        normalization_version="normalizer/v1",
        source_hash="sha256-test",
        parser_fingerprint="sha256:parser",
        profile_fingerprint="sha256:profile",
        content_checksum="sha256:raw",
        pages=(DocumentPage(pdf_page_number=1, width=612, height=792, dimension_unit="pt"),),
        blocks=(DocumentBlock(
            block_id="fallback-source", page_number=1, kind="text", source_order=0,
            reading_order=0, order_status="preserved", text="Fallback body.",
            source_locator="paper://paper-1#page=1",
            bbox_unknown_reason="geometry unavailable", content_owner_id="fallback-source",
        ),),
    )
    document = make_doc(sections=[
        make_section("only", "Body", "Fallback body."),
    ]).model_copy(update={"fidelity": fidelity})

    paragraph = next(chunk for chunk in CHUNKER.chunk(document, "marker") if chunk.chunk_type == "paragraph")

    source_map = paragraph.metadata["fidelity_source_map"]
    assert source_map["mapping_status"] == "unavailable"
    assert source_map["reason"] == "paragraph_source_range_unavailable"
    assert source_map["blocks"] == []


def test_fidelity_chunk_source_map_reports_truncation_instead_of_silently_omitting_blocks():
    source_ref = "paper://large-document/pdf"
    blocks = tuple(DocumentBlock(
        block_id=f"block-{index}", page_number=1, kind="text", source_order=index,
        reading_order=index, order_status="preserved",
        source_locator=f"{source_ref}#page=1&block={index}",
        bbox_unknown_reason="geometry unavailable", content_owner_id=f"block-{index}",
    ) for index in range(129))
    fidelity = DocumentFidelity(
        schema_version="research-document-fidelity.v1", normalization_version="normalizer/v1",
        source_hash="sha256-large", parser_fingerprint="sha256:parser",
        profile_fingerprint="sha256:profile", content_checksum="sha256:raw",
        pages=(DocumentPage(pdf_page_number=1, width=612, height=792, dimension_unit="pt"),),
        blocks=blocks,
    )
    document = make_doc(sections=[make_section("large", "Large", "Content")]).model_copy(update={
        "source_hash": "sha256-large", "fidelity": fidelity,
    })
    marker_metadata = {"marker_provenance": {
        "blocks": [{"block_id": f"block-{index}"} for index in range(129)],
    }}

    source_map = _fidelity_chunk_source_map(document, metadata=marker_metadata)

    assert source_map is not None
    assert source_map["mapping_status"] == "partial"
    assert source_map["expected_block_count"] == 129
    assert source_map["matched_block_count"] == 129
    assert source_map["omitted_block_count"] == 1
    assert len(source_map["blocks"]) == 128


def test_fidelity_chunk_source_map_orders_pages_before_page_local_order():
    blocks = tuple(DocumentBlock(
        block_id=f"p{page}-b{index}", page_number=page, kind="text", source_order=index,
        reading_order=index, order_status="preserved", source_locator=f"paper://test#page={page}",
        bbox_unknown_reason="unavailable", content_owner_id=f"p{page}-b{index}",
    ) for page in (1, 2) for index in (0, 1))
    fidelity = DocumentFidelity(
        schema_version="research-document-fidelity.v1", normalization_version="normalizer/v1",
        source_hash="sha256-test", parser_fingerprint="sha256:parser",
        profile_fingerprint="sha256:profile", content_checksum="sha256:raw",
        pages=tuple(DocumentPage(pdf_page_number=page, width=612, height=792, dimension_unit="pt")
                    for page in (1, 2)), blocks=blocks,
    )
    document = make_doc().model_copy(update={"fidelity": fidelity})
    mapping = _fidelity_chunk_source_map(document, metadata={"marker_provenance": {
        "blocks": [{"block_id": block.block_id} for block in reversed(blocks)],
    }})
    assert mapping["mapping_status"] == "complete"
    assert [entry["block_id"] for entry in mapping["blocks"]] == [
        "p1-b0", "p1-b1", "p2-b0", "p2-b1",
    ]


def test_fidelity_chunk_math_map_caps_total_spans_and_reports_omissions():
    blocks = tuple(DocumentBlock(
        block_id=f"math-{index}", page_number=1, kind="text", source_order=index,
        reading_order=index, order_status="preserved", source_locator="paper://test#page=1",
        bbox_unknown_reason="unavailable", content_owner_id=f"math-{index}", text="x" * 100,
        inline_math=tuple(InlineMathSpan(
            start_offset=i, end_offset=i + 1, offset_text_version="normalized",
            raw_latex="x", source_locator="paper://test#page=1",
        ) for i in range(100)),
    ) for index in (0, 1))
    fidelity = DocumentFidelity(
        schema_version="research-document-fidelity.v1", normalization_version="normalizer/v1",
        source_hash="sha256-test", parser_fingerprint="sha256:parser",
        profile_fingerprint="sha256:profile", content_checksum="sha256:raw",
        pages=(DocumentPage(pdf_page_number=1, width=612, height=792, dimension_unit="pt"),),
        blocks=blocks,
    )
    document = make_doc().model_copy(update={"fidelity": fidelity})
    mapping = _fidelity_chunk_source_map(document, metadata={"marker_provenance": {
        "blocks": [{"block_id": block.block_id} for block in blocks],
    }})
    assert mapping["mapping_status"] == "partial"
    assert mapping["omitted_block_count"] == 0
    assert mapping["omitted_inline_math_span_count"] == 72
    assert [len(entry["inline_math_spans"]) for entry in mapping["blocks"]] == [100, 28]
    assert mapping["blocks"][1]["inline_math_span_count"] == 100
    assert mapping["blocks"][1]["omitted_inline_math_span_count"] == 72
