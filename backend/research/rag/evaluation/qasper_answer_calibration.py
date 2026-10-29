"""Human calibration of the QASPER answer judge.

A calibration set is drawn deterministically from a completed answer run: only
answered cases with a valid judge verdict qualify, balanced across answer-type
strata and the judge's own correct/incorrect verdict so both error directions of
the judge are observable. Humans label the exported sheet; ``score_calibration``
then measures how far the judge can be trusted.
"""
from __future__ import annotations

import csv
import hashlib
import io
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

CALIBRATION_PROTOCOL = "qasper_answer_judge_calibration_v1"
LABEL_COLUMNS = (
    "case_no",
    "case_id",
    "judge_correct",
    "human_correct",
    "human_unsupported_citations",
    "notes",
)
_TRUE = {"yes", "y", "true", "1", "是", "对"}
_FALSE = {"no", "n", "false", "0", "否", "错"}


class CalibrationLabelError(ValueError):
    """The filled label sheet does not match the exported calibration set."""


@dataclass(frozen=True)
class CitedPassage:
    index: int
    chunk_id: str
    text: str
    judge_supports: bool | None


@dataclass(frozen=True)
class CalibrationCase:
    case_no: int
    case_id: str
    stratum: str
    question: str
    references: tuple[str, ...]
    answer: str
    judge_correct: bool
    judge_rationale: str
    passages: tuple[CitedPassage, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_no": self.case_no,
            "case_id": self.case_id,
            "stratum": self.stratum,
            "question": self.question,
            "references": list(self.references),
            "answer": self.answer,
            "judge_correct": self.judge_correct,
            "judge_rationale": self.judge_rationale,
            "passages": [
                {"index": p.index, "chunk_id": p.chunk_id, "text": p.text, "judge_supports": p.judge_supports}
                for p in self.passages
            ],
        }


def _judged(case: Mapping[str, Any]) -> bool:
    judge = case.get("judge")
    return (
        case.get("outcome") == "answered"
        and isinstance(judge, Mapping)
        and not judge.get("error")
        and isinstance(judge.get("correct"), bool)
    )


def select_calibration_cases(
    cases: Sequence[Mapping[str, Any]], *, size: int, seed: str,
) -> list[Mapping[str, Any]]:
    """Pick ``size`` judged cases, round-robin over (stratum, judge verdict) cells."""
    if size < 1:
        raise ValueError("calibration size must be positive")
    cells: dict[tuple[str, bool], list[Mapping[str, Any]]] = defaultdict(list)
    for case in cases:
        if _judged(case):
            cells[(str(case["stratum"]), bool(case["judge"]["correct"]))].append(case)
    for members in cells.values():
        members.sort(key=lambda case: hashlib.sha256(f"{seed}:{case['case_id']}".encode()).hexdigest())
    order = sorted(cells)
    selected: list[Mapping[str, Any]] = []
    depth = 0
    while len(selected) < size and any(depth < len(cells[key]) for key in order):
        for key in order:
            if depth < len(cells[key]) and len(selected) < size:
                selected.append(cells[key][depth])
        depth += 1
    return selected


def build_calibration_cases(
    selected: Sequence[Mapping[str, Any]], *, passage_text: Callable[[str], str],
) -> tuple[CalibrationCase, ...]:
    """Attach the text of every passage the answer cited, keyed by its marker index."""
    built = []
    for number, case in enumerate(selected, start=1):
        context_ids = list(case.get("context_chunk_ids") or ())
        verdicts = {int(item["index"]): bool(item["supports"]) for item in case["judge"].get("citations", ())}
        passages = []
        for index in sorted(verdicts):
            if not 1 <= index <= len(context_ids):
                raise ValueError(f"case {case['case_id']} cites [{index}] outside its context")
            chunk_id = context_ids[index - 1]
            passages.append(CitedPassage(index, chunk_id, passage_text(chunk_id), verdicts[index]))
        built.append(CalibrationCase(
            case_no=number,
            case_id=str(case["case_id"]),
            stratum=str(case["stratum"]),
            question=str(case["question"]),
            references=tuple(str(ref) for ref in case.get("references", ())),
            answer=str(case.get("answer") or ""),
            judge_correct=bool(case["judge"]["correct"]),
            judge_rationale=str(case["judge"].get("rationale") or ""),
            passages=tuple(passages),
        ))
    return tuple(built)


def render_calibration_sheet(cases: Sequence[CalibrationCase], *, run_directory: str) -> str:
    lines = [
        "# QASPER judge 人工校准",
        "",
        f"来源运行：`{run_directory}`，共 {len(cases)} 题。",
        "",
        "在 `labels.csv`（由 `labels.template.csv` 复制）中逐题填写：",
        "",
        "- `human_correct`：回答是否与参考答案在实质上一致，填 yes / no。",
        "- `human_unsupported_citations`：你认为不能支撑回答的引用编号，用分号分隔，如 `2;3`；全部支撑则留空。",
        "- `notes`：可选备注。",
        "",
        "judge 的判断附在每题末尾，建议先独立判断，再对照。",
        "",
    ]
    for case in cases:
        lines += [
            f"## {case.case_no}. [{case.stratum}] {case.question}",
            "",
            f"case_id: `{case.case_id}`",
            "",
            "**参考答案**",
            "",
            *[f"- {reference}" for reference in case.references],
            "",
            "**系统回答**",
            "",
            case.answer,
            "",
            "**引用段落**",
            "",
        ]
        for passage in case.passages:
            lines += [f"[{passage.index}] `{passage.chunk_id}`", "", f"> {_quote(passage.text)}", ""]
        supports = ", ".join(
            f"[{p.index}] {'支撑' if p.judge_supports else '不支撑'}" for p in case.passages
        ) or "无引用"
        lines += [
            "<details><summary>judge 判断</summary>",
            "",
            f"正确：{'是' if case.judge_correct else '否'}；引用：{supports}",
            "",
            case.judge_rationale,
            "",
            "</details>",
            "",
        ]
    return "\n".join(lines)


def render_label_template(cases: Sequence[CalibrationCase]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(LABEL_COLUMNS)
    for case in cases:
        writer.writerow([case.case_no, case.case_id, "yes" if case.judge_correct else "no", "", "", ""])
    return buffer.getvalue()


def score_calibration(cases: Sequence[Mapping[str, Any]], labels_csv: str) -> dict[str, Any]:
    """Compare filled human labels with the judge; unlabeled rows are reported, not guessed."""
    by_id = {str(case["case_id"]): case for case in cases}
    rows = list(csv.DictReader(io.StringIO(labels_csv.lstrip("﻿"))))
    if not rows or set(LABEL_COLUMNS) - set(rows[0]):
        raise CalibrationLabelError(f"labels must have columns {', '.join(LABEL_COLUMNS)}")
    matrix = {(True, True): 0, (True, False): 0, (False, True): 0, (False, False): 0}
    citation_agree = citation_total = 0
    unlabeled: list[str] = []
    disagreements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        case_id = row["case_id"].strip()
        if case_id not in by_id:
            raise CalibrationLabelError(f"label row references unknown case {case_id}")
        if case_id in seen:
            raise CalibrationLabelError(f"case {case_id} is labeled twice")
        seen.add(case_id)
        case = by_id[case_id]
        human = _parse_bool(row["human_correct"], case_id)
        if human is None:
            unlabeled.append(case_id)
            continue
        judge = bool(case["judge_correct"])
        matrix[(judge, human)] += 1
        if judge != human:
            disagreements.append({"case_id": case_id, "judge_correct": judge, "human_correct": human,
                                  "notes": row.get("notes", "").strip()})
        human_unsupported = _parse_indexes(row["human_unsupported_citations"], case_id)
        cited = {int(p["index"]): p["judge_supports"] for p in case["passages"]}
        if human_unsupported - set(cited):
            raise CalibrationLabelError(f"case {case_id} marks citations it does not have")
        for index, judge_supports in cited.items():
            citation_total += 1
            citation_agree += (index not in human_unsupported) == bool(judge_supports)
    labeled = sum(matrix.values())
    return {
        "protocol": CALIBRATION_PROTOCOL,
        "labeled": labeled,
        "unlabeled": unlabeled,
        "correctness_agreement": _ratio(matrix[(True, True)] + matrix[(False, False)], labeled),
        "cohen_kappa": _kappa(matrix),
        # Judge says correct but the human disagrees: the judge inflates accuracy.
        "judge_false_accept": _ratio(matrix[(True, False)], matrix[(True, False)] + matrix[(False, False)]),
        "judge_false_reject": _ratio(matrix[(False, True)], matrix[(False, True)] + matrix[(True, True)]),
        "confusion": {f"judge_{'yes' if j else 'no'}__human_{'yes' if h else 'no'}": n
                      for (j, h), n in matrix.items()},
        "citation_agreement": _ratio(citation_agree, citation_total),
        "disagreements": disagreements,
    }


def _parse_bool(value: str, case_id: str) -> bool | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise CalibrationLabelError(f"case {case_id}: human_correct must be yes or no, got {value!r}")


def _parse_indexes(value: str, case_id: str) -> set[int]:
    parts = [part.strip() for part in (value or "").replace(",", ";").replace("，", ";").split(";")]
    try:
        return {int(part.strip("[]")) for part in parts if part}
    except ValueError as exc:
        raise CalibrationLabelError(f"case {case_id}: citation indexes must be integers") from exc


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def _kappa(matrix: Mapping[tuple[bool, bool], int]) -> float | None:
    total = sum(matrix.values())
    if not total:
        return None
    observed = (matrix[(True, True)] + matrix[(False, False)]) / total
    judge_yes = (matrix[(True, True)] + matrix[(True, False)]) / total
    human_yes = (matrix[(True, True)] + matrix[(False, True)]) / total
    expected = judge_yes * human_yes + (1 - judge_yes) * (1 - human_yes)
    return None if expected == 1 else (observed - expected) / (1 - expected)


def _quote(text: str) -> str:
    return " ".join(text.split())


__all__ = [
    "CALIBRATION_PROTOCOL",
    "CalibrationCase",
    "CalibrationLabelError",
    "CitedPassage",
    "LABEL_COLUMNS",
    "build_calibration_cases",
    "render_calibration_sheet",
    "render_label_template",
    "score_calibration",
    "select_calibration_cases",
]
