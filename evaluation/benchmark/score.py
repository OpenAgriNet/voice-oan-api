"""Aggregate scored runs into the reporting workbook.

    python evaluation/benchmark/score.py --new scored_ab01_prod.jsonl \
                                         --old scored_ab01_local.jsonl

Sheets:
  1. Summary            metrics down the rows, Old Prompt vs New Prompt across
                        the columns; then recurring-issue rates, then per-category
  2. New Prompt Detail  per-question rows for the current (HEAD) prompt
  3. Old Prompt Detail  per-question rows for the baseline (cb529f2) prompt
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from common import RUNS_DIR

OLD_LABEL = "Old Prompt"
NEW_LABEL = "New Prompt"

# Order requested in the metric sheet.
DETAIL_COLUMNS = [
    "file", "session_id", "questions_answers", "question", "answer", "tool_calls",
    "score_translation_accuracy", "score_tool_call_quality",
    "reason_translation_accuracy", "reason_tool_call_quality",
    "score_accuracy_completeness", "score_actionability", "score_conversation_closure",
    "score_source_data_comprehensiveness", "score_no_fabrication",
    "score_citation_accuracy", "score_citation_comprehensiveness",
    "score_grammar_fluency", "score_language_purity", "score_brevity",
    "score_output_hygiene", "score_elapsed_seconds", "score_word_count", "score_error",
    "reason_accuracy_completeness", "reason_actionability", "reason_conversation_closure",
    "reason_source_data_comprehensiveness", "reason_no_fabrication",
    "reason_citation_accuracy", "reason_citation_comprehensiveness",
    "reason_grammar_fluency", "reason_language_purity", "reason_brevity",
    "reason_output_hygiene",
]

# (row label, scored column) in the order given in the metric sheet.
HEADLINE_ROWS = [
    ("Tool call quality ( max: 4)", "score_tool_call_quality"),
    ("Translation quality (max: 10)", "score_translation_accuracy"),
    ("Accuracy_completeness  ( max: 4)", "score_accuracy_completeness"),
    ("Brevity  ( max: 4)", "score_brevity"),
    ("No_Fabrication  ( max: 1)", "score_no_fabrication"),
    ("Grammar_Fluency  ( max: 4)", "score_grammar_fluency"),
    ("Language purity  ( max: 4)", "score_language_purity"),
]

SUPPORTING_ROWS = [
    ("Actionability ( max: 4)", "score_actionability"),
    ("Source data comprehensiveness ( max: 4)", "score_source_data_comprehensiveness"),
    ("Output hygiene ( max: 4)", "score_output_hygiene"),
    ("Conversation closure ( max: 2)", "score_conversation_closure"),
]

ISSUE_ROWS = [
    ("No Information Given", "score_answered_issue", "zero"),
    ("False Out-of-Scope Refusal", "score_no_false_refusal_issue", "zero"),
    ("Incorrect / Hallucinated (fabrication)", "score_no_fabrication", "zero"),
    ("Unrelated / Off-Topic Answer", "score_relevance_issue", "low"),
    ("Overly Generic Response", "score_specificity_issue", "low"),
]


def load(path: Path) -> pd.DataFrame:
    if not path.is_absolute():
        path = RUNS_DIR / path.name
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    frame = pd.DataFrame(records)
    frame["file"] = path.name
    frame["questions_answers"] = frame["question"] + "\n---\n" + frame["answer"].fillna("")
    frame["tool_calls"] = frame["tool_names"].apply(
        lambda names: ", ".join(names) if isinstance(names, list) and names else "NONE"
    )
    frame["score_elapsed_seconds"] = frame["elapsed_seconds"]
    frame["score_word_count"] = frame["word_count"]
    frame["score_error"] = frame["error"]
    for column in DETAIL_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    return frame


def _mean(frame: pd.DataFrame, column: str) -> float | None:
    series = pd.to_numeric(frame.get(column), errors="coerce").dropna()
    return round(series.mean(), 3) if len(series) else None


def _rate(frame: pd.DataFrame, column: str, mode: str) -> float | None:
    series = pd.to_numeric(frame.get(column), errors="coerce").dropna()
    if not len(series):
        return None
    hit = (series <= 2).mean() if mode == "low" else (series == 0).mean()
    return round(100 * hit, 1)


def _delta(old: float | None, new: float | None) -> float | str:
    if old is None or new is None:
        return ""
    return round(new - old, 3)


def build_summary(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    old_ok, new_ok = old[old["error"] == ""], new[new["error"] == ""]
    rows = [{"Metric": "Language", OLD_LABEL: "mr", NEW_LABEL: "mr", "Change": ""}]

    for label, column in HEADLINE_ROWS + SUPPORTING_ROWS:
        old_value, new_value = _mean(old_ok, column), _mean(new_ok, column)
        rows.append({
            "Metric": label,
            OLD_LABEL: old_value,
            NEW_LABEL: new_value,
            "Change": _delta(old_value, new_value),
        })

    old_zero = round(100 * (old_ok["n_tool_calls"] == 0).mean(), 1)
    new_zero = round(100 * (new_ok["n_tool_calls"] == 0).mean(), 1)
    old_fab = _rate(old_ok, "score_no_fabrication", "zero")
    new_fab = _rate(new_ok, "score_no_fabrication", "zero")

    extras = [
        ("Answers with NO tool call (%)", old_zero, new_zero),
        ("Fabrication rate (%)", old_fab, new_fab),
        ("Mean latency (seconds)", round(pd.to_numeric(old_ok["elapsed_seconds"]).mean(), 2),
         round(pd.to_numeric(new_ok["elapsed_seconds"]).mean(), 2)),
        ("Mean answer length (words)", round(pd.to_numeric(old_ok["word_count"]).mean(), 1),
         round(pd.to_numeric(new_ok["word_count"]).mean(), 1)),
        ("Questions attempted", len(old), len(new)),
        ("Questions answered without error", len(old_ok), len(new_ok)),
    ]
    for label, old_value, new_value in extras:
        rows.append({
            "Metric": label,
            OLD_LABEL: old_value,
            NEW_LABEL: new_value,
            "Change": _delta(old_value, new_value),
        })
    return pd.DataFrame(rows)


def build_issues(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    old_ok, new_ok = old[old["error"] == ""], new[new["error"] == ""]
    rows = []
    for label, column, mode in ISSUE_ROWS:
        old_value, new_value = _rate(old_ok, column, mode), _rate(new_ok, column, mode)
        rows.append({
            "Issue (from field report)": label,
            f"{OLD_LABEL} (%)": old_value,
            f"{NEW_LABEL} (%)": new_value,
            "Change": _delta(old_value, new_value),
        })
    return pd.DataFrame(rows)


def build_by_category(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    old_ok, new_ok = old[old["error"] == ""], new[new["error"] == ""]
    rows = []
    for category in sorted(set(old_ok["category"]) | set(new_ok["category"])):
        old_group = old_ok[old_ok["category"] == category]
        new_group = new_ok[new_ok["category"] == category]
        rows.append({
            "Category": category,
            "n": len(new_group) or len(old_group),
            f"No tool call % - {OLD_LABEL}": round(100 * (old_group["n_tool_calls"] == 0).mean(), 1),
            f"No tool call % - {NEW_LABEL}": round(100 * (new_group["n_tool_calls"] == 0).mean(), 1),
            f"Tool call quality - {OLD_LABEL}": _mean(old_group, "score_tool_call_quality"),
            f"Tool call quality - {NEW_LABEL}": _mean(new_group, "score_tool_call_quality"),
            f"Fabrication % - {OLD_LABEL}": _rate(old_group, "score_no_fabrication", "zero"),
            f"Fabrication % - {NEW_LABEL}": _rate(new_group, "score_no_fabrication", "zero"),
        })
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--new", required=True, help="scored jsonl for the new (HEAD) prompt")
    parser.add_argument("--old", required=True, help="scored jsonl for the old (cb529f2) prompt")
    parser.add_argument("--out", default="benchmark_results.xlsx")
    args = parser.parse_args()

    new = load(Path(args.new))
    old = load(Path(args.old))

    summary = build_summary(old, new)
    issues = build_issues(old, new)
    by_category = build_by_category(old, new)

    out_path = RUNS_DIR / args.out
    detail_columns = ["category"] + DETAIL_COLUMNS
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        row = 0
        for title, table in [
            ("SUMMARY METRICS - OLD PROMPT vs NEW PROMPT", summary),
            ("RECURRING ISSUE RATES (lower is better)", issues),
            ("BY QUESTION CATEGORY", by_category),
        ]:
            pd.DataFrame([[title]]).to_excel(
                writer, sheet_name="Summary", index=False, header=False, startrow=row
            )
            table.to_excel(writer, sheet_name="Summary", index=False, startrow=row + 1)
            row += len(table) + 4

        new[detail_columns].to_excel(writer, sheet_name="New Prompt Detail", index=False)
        old[detail_columns].to_excel(writer, sheet_name="Old Prompt Detail", index=False)

        summary_sheet = writer.sheets["Summary"]
        summary_sheet.column_dimensions["A"].width = 42
        for column in ("B", "C", "D", "E", "F", "G", "H", "I"):
            summary_sheet.column_dimensions[column].width = 20

        for sheet_name in ("New Prompt Detail", "Old Prompt Detail"):
            worksheet = writer.sheets[sheet_name]
            worksheet.freeze_panes = "E2"
            for column, width in [("D", 55), ("E", 70), ("F", 30)]:
                worksheet.column_dimensions[column].width = width

    print(f"wrote {out_path}\n")
    print(summary.to_string(index=False))
    print()
    print(issues.to_string(index=False))


if __name__ == "__main__":
    main()
