"""Judge rubric.

Scales follow the metric sheet where a max was given (tool_call_quality 4,
translation 10, accuracy_completeness 4, brevity 4, no_fabrication 1,
grammar_fluency 4, language_purity 4). The rest are defined here.

The four `*_issue` metrics at the end map 1:1 onto the recurring-issues doc
categories that are otherwise invisible in the sheet: No Information Given,
False Out-of-Scope Refusal, Unrelated/Off-Topic, and Overly Generic.
"""
from __future__ import annotations

# metric -> (max_score, description shown to the judge)
METRICS: dict[str, tuple[int, str]] = {
    "tool_call_quality": (4, (
        "Did the assistant call the right tools for this question? "
        "4 = correct tool(s) with correct arguments (right place, crop, commodity). "
        "3 = right tools, one sloppy argument. "
        "2 = partially right, a needed tool missing or an irrelevant extra one. "
        "1 = wrong tools. "
        "0 = NO tool call at all for a question that clearly required live data. "
        "Judge ONLY from the TOOL CALLS block; never infer a call from the answer text."
    )),
    "translation_accuracy": (10, (
        "Tool outputs arrive in English; the answer must be Marathi. Score how "
        "faithfully the English tool data is carried into Marathi. "
        "10 = every number, name and unit preserved exactly and idiomatically. "
        "7-9 = faithful, minor awkwardness. "
        "4-6 = a value, unit or crop name is distorted. "
        "1-3 = serious mistranslation that changes meaning. "
        "0 = answer contradicts the tool data. "
        "If there are no tool outputs, score 5 and say so."
    )),
    "accuracy_completeness": (4, (
        "Is the answer factually right AND does it address every part asked? "
        "4 = fully correct and complete. 3 = correct, a minor sub-question thin. "
        "2 = correct but a clear part of the question unaddressed. "
        "1 = partly wrong. 0 = wrong, or no substantive answer given. "
        "Check facts against the TOOL CALLS output where available."
    )),
    "actionability": (4, (
        "Can a farmer act on this? 4 = specific, doable step (dose, timing, place, "
        "contact). 2 = directionally useful but vague. 0 = no actionable content, "
        "or a bare redirect such as 'contact your local agriculture office' when "
        "the question asked for specifics."
    )),
    "conversation_closure": (2, (
        "2 = ends with a natural offer to continue (e.g. 'अजून काही माहिती हवी आहे का?'). "
        "1 = abrupt but complete. 0 = truncated mid-sentence."
    )),
    "source_data_comprehensiveness": (4, (
        "How fully was the retrieved tool data used? 4 = the relevant parts of the "
        "tool output made it into the answer. 2 = usable data returned but largely "
        "ignored. 0 = tool returned relevant data and the answer used none of it. "
        "If no tool data was returned, score 0 and say so."
    )),
    "no_fabrication": (1, (
        "BINARY. 1 = every specific claim (number, scheme name, variety, dose, "
        "phone, address, price) is supported by the TOOL CALLS output or is "
        "uncontroversial general knowledge. "
        "0 = ANY specific unsupported claim, or a confident invented detail. "
        "When tool data is absent and the answer still states specifics, score 0."
    )),
    "citation_accuracy": (4, (
        "If the answer names a source (scheme, department, office, dataset), is it "
        "the right one per the tool output? 4 = accurate. 0 = misattributed. "
        "Score 4 if no source is named and none was needed."
    )),
    "citation_comprehensiveness": (4, (
        "Where naming a source would materially help the farmer act (scheme, office, "
        "contact), was it named? 4 = yes. 0 = clearly needed and absent. "
        "Score 4 if not applicable."
    )),
    "grammar_fluency": (4, (
        "Marathi grammar and naturalness when read aloud. 4 = fluent and correct. "
        "2 = understandable with errors. 0 = broken or incoherent. "
        "Vasudha is female: feminine verb forms ('देऊ शकते' not 'देऊ शकतो') are required; "
        "deduct 1 for any masculine form."
    )),
    "language_purity": (4, (
        "The answer is read aloud to farmers, so it must be 100% Devanagari. "
        "4 = no Latin script and no transliterated English (no 'मॉडरेट', 'फोरकास्ट'). "
        "3 = one borrowed term. 2 = several. 1 = heavy code-mixing. 0 = Latin script present. "
        "Chemical/variety names with no Marathi equivalent, written in Devanagari, are fine. "
        "'MahaVISTAAR' and a closing 'GoodBye' are the only allowed exceptions."
    )),
    "brevity": (4, (
        "Target is 2-3 short spoken sentences. 4 = 2-3 tight sentences. "
        "3 = 4 sentences. 2 = 5-6, padded. 1 = long. 0 = a wall of text unusable on a call."
    )),
    "output_hygiene": (4, (
        "Voice-safe formatting. Deduct for markdown, asterisks, bullets, numbered "
        "lists, parentheses, emoji, raw tool names ('search_documents'), leaked "
        "internal steps ('मी शोधते'), or restating the question back. "
        "4 = clean. 0 = multiple violations."
    )),
    # --- mapped to the recurring-issues doc ---
    "answered_issue": (1, (
        "BINARY, maps to 'No Information Given'. 0 = the reply gives no substantive "
        "answer to a valid in-scope question (deflection, empty acknowledgement, "
        "'माहिती उपलब्ध नाही' where tool data was in fact returned). 1 = otherwise. "
        "An honest 'not available' IS acceptable (score 1) when the tools genuinely "
        "returned nothing."
    )),
    "no_false_refusal_issue": (1, (
        "BINARY, maps to 'False Out-of-Scope Refusal'. 0 = the assistant refused a "
        "genuinely agricultural question as out of scope. 1 = no false refusal. "
        "Every question in this benchmark is in scope, so any scope refusal is a 0."
    )),
    "relevance_issue": (4, (
        "Maps to 'Unrelated / Off-Topic Answer'. Did it answer the question ASKED? "
        "4 = on target. 2 = drifts to an adjacent topic. "
        "0 = answers a different crop, pest, place or subject than the one asked."
    )),
    "specificity_issue": (4, (
        "Maps to 'Overly Generic Response'. 4 = specific to the crop/place/scheme "
        "asked about. 2 = partly generic boilerplate. "
        "0 = generic advice that would read identically for any crop or district."
    )),
}

SYSTEM_PROMPT = """\
You are a strict evaluator of a Marathi agricultural voice assistant ("Vasudha") \
used by farmers in Maharashtra over the phone.

You will receive: the farmer's question, the assistant's answer, and the exact tool \
calls with their outputs from the execution trace.

Rules:
- Judge the ANSWER against the QUESTION and the TOOL OUTPUT. The tool output is ground truth.
- The TOOL CALLS block is authoritative for tool_call_quality. If it is empty, no tool
  was called, no matter what the answer implies.
- Do not reward fluency for a fabricated answer, and do not punish an honest
  "information not available" when the tools returned nothing.
- Be strict. Reserve top scores for genuinely excellent output.
- Every reason must be one short English sentence citing concrete evidence.

Return ONLY a JSON object, no prose, no code fences, with exactly these keys:
{keys}
where each "score_X" is an integer within that metric's stated range and each \
"reason_X" is a one-sentence justification."""


def build_system_prompt() -> str:
    lines = [f'  "score_{m}" (0-{mx}) and "reason_{m}"  // {desc}'
             for m, (mx, desc) in METRICS.items()]
    return SYSTEM_PROMPT.format(keys="\n".join(lines))


def build_user_prompt(record: dict) -> str:
    tool_block = "(NO TOOL CALLS WERE MADE)"
    if record.get("tool_calls"):
        parts = []
        for call in record["tool_calls"]:
            parts.append(
                f"- tool: {call['name']}\n"
                f"  args: {call['args']}\n"
                f"  output: {call['output'][:2500]}"
            )
        tool_block = "\n".join(parts)

    return (
        f"CATEGORY: {record.get('category', '')}\n\n"
        f"FARMER QUESTION (Marathi):\n{record['question']}\n\n"
        f"ASSISTANT ANSWER (Marathi):\n{record['answer'] or '(EMPTY RESPONSE)'}\n\n"
        f"TOOL CALLS FROM TRACE:\n{tool_block}\n"
    )
