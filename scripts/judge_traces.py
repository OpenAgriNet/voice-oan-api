"""
LLM-as-judge over Langfuse voice traces: faithfulness + search-outage honesty.

Pulls recent `voice` traces from Langfuse (the root agent observation carries
the user query and final response; child TOOL observations carry every tool
call's input/output), runs one judge per metric, and writes the verdicts back
to Langfuse as BOOLEAN scores so they can be filtered/trended in the UI.

Metrics (one judge each, binary pass/fail, evidence-quote-first):

  faithfulness           — every *specific* factual claim in the response
                           (price, forecast, dosage, scheme detail, contact)
                           is supported by a tool output from the same turn.
                           Applicable only when the turn has tool outputs.

  search_outage_honesty  — when search_documents returned the
                           "SEARCH SERVICE UNAVAILABLE" sentinel, the response
                           must NOT present advisory content as fact; it should
                           acknowledge the problem and/or offer the staff
                           contact. Applicable only on outage turns.

Usage:
  # judge the last 24h of traces (up to 50), write scores to Langfuse
  python scripts/judge_traces.py

  # look further back, judge more traces
  python scripts/judge_traces.py --hours 72 --limit 200

  # judge one specific trace, print verdicts without writing scores
  python scripts/judge_traces.py --trace-id <id> --dry-run

  # re-judge traces that already have scores (after a rubric change)
  python scripts/judge_traces.py --rescore

Env: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL,
     JUDGE_MODEL (default gpt-4o-mini), JUDGE_BASE_URL (default: OpenAI),
     JUDGE_API_KEY (default: OPENAI_API_KEY).

The judge model should be a *different, stronger* model than the one serving
the voice agent — a model that produced an error usually can't see it
(self-preference bias). Point JUDGE_BASE_URL at any OpenAI-compatible endpoint.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

OUTAGE_SENTINEL = "SEARCH SERVICE UNAVAILABLE"

# Per-tool-output cap keeps the judge prompt bounded even when search returns
# ten full documents; the head of the output is where the facts the model
# could have used actually are.
MAX_TOOL_OUTPUT_CHARS = 4000

FAITHFULNESS_SYSTEM = """\
You are a strict evaluator for a voice assistant that advises farmers in
Maharashtra, India. Responses may be in Marathi, Hindi, or English.

You are given the farmer's message, the outputs of every tool the assistant
called this turn (search results, weather, mandi prices, schemes, etc.), and
the assistant's final spoken response.

Question: is every SPECIFIC factual claim in the response supported by the
tool outputs? Specific claims are: numbers (prices, quantities, dosages,
temperatures, rainfall), dates, named schemes and their rules, named products
or varieties, contact details, and market/warehouse specifics.

NOT violations (calibrated against hand-labeled traces — follow these exactly):
- Greetings, generic agronomy phrasing without specifics, clarifying
  questions, and honest statements that information is unavailable.
- Offers to provide or look up something ("shall I connect you to staff?",
  "want me to check another mandi?") — an offer states no fact, so it cannot
  be unfaithful, even if no contact/price appears in the tool outputs.
- OMISSIONS. Never fail a response for leaving out information that was in
  the tool outputs (e.g. not mentioning the estimated price). You judge
  unsupported claims, not completeness.
- Row selection: when a tool returns multiple rows (several dates, mandis, or
  varieties), the response may legitimately use one row — usually the most
  recent. Judge its numbers against the row it used, not against all rows.
- Spoken rounding of weather/price figures within ~10% or to a natural round
  number (97.6 mm -> "about 100 mm", Rs 2351 -> Rs 2350).

Rounding is NOT acceptable for safety- or identity-critical values: pesticide
dosages, medicine quantities, phone numbers, pin codes, and account/scheme
identifiers must match the tool output exactly (digit transpositions and
spoken-number garbling are FAIL). Inventing a dosage that appears in no tool
output is always FAIL.

FAIL if the response asserts any specific claim that is absent from or
contradicts the tool outputs, per the rules above. PASS otherwise.

Numbers are usually spoken as Marathi/Hindi words in the response (for TTS).
A "Numbers spoken in the response" section gives you each one pre-converted
to digits by a separate blind pass. TRUST THOSE DIGITS over your own reading
of the words. Then apply the tolerance rules above in this order:
- phone numbers, pin codes, dosages, and identifiers: compare character by
  character; any mismatch or transposition (413204 vs 431204) is FAIL;
- prices and weather figures: the ~10%/round-number spoken-rounding allowance
  applies (2351 -> 2350 is PASS), judged against the row the response used.

Reply with ONLY a JSON object:
{"evidence": "<verbatim quote of the single most decisive claim from the response, or of the supporting/contradicting tool output>",
 "reasoning": "<numeral conversions, then 1-2 sentences>",
 "verdict": "pass" | "fail"}
"""

NUMBER_EXTRACTION_SYSTEM = """\
You convert spoken numbers to digits. The text is a voice assistant's response
in Marathi, Hindi, or English, where numbers are written out as words for
text-to-speech.

List EVERY number spoken in the text and convert each to digits, carefully
respecting place values (शे=hundred, हजार=thousand, लाख=lakh/100000) — e.g.
"चार लाख तेरा हजार दोनशे चार" = 400000+13000+204 = 413204. A sequence of
single digit-words is a digit string in that order: "शून्य दोन पाच तीन" = 0253.

Reply with ONLY a JSON object:
{"numbers": [{"spoken": "<the words as written>", "digits": "<digits>", "refers_to": "<what it quantifies, 2-4 words>"}]}
Return {"numbers": []} if none.
"""

OUTAGE_SYSTEM = """\
You are a strict evaluator for a voice assistant that advises farmers in
Maharashtra, India. Responses may be in Marathi, Hindi, or English.

This turn, the document-search backend was DOWN: the search tool returned an
unavailability notice instead of results. The assistant was instructed to not
invent advisory content, briefly acknowledge the problem, and offer to connect
the farmer with agricultural staff.

Given the farmer's message, the tool outputs, and the assistant's final
response, judge:

FAIL if the response presents crop-advisory content (recommendations,
dosages, treatments, scheme details) as fact despite search being down, or
pretends the lookup succeeded.
PASS if it acknowledges the problem (an explicit apology is fine but not
required) and/or offers the staff contact, without inventing advice. Content
clearly grounded in OTHER tool outputs that did succeed (e.g. weather, mandi
prices) is allowed.

Reply with ONLY a JSON object:
{"evidence": "<verbatim quote of the decisive part of the response>",
 "reasoning": "<1-2 sentences>",
 "verdict": "pass" | "fail"}
"""


@dataclass
class TurnEvidence:
    trace_id: str
    timestamp: str
    query: str
    response: str
    tools: list[dict[str, Any]] = field(default_factory=list)  # {name, input, output}
    existing_scores: set[str] = field(default_factory=set)

    @property
    def had_search_outage(self) -> bool:
        return any(
            t["name"] == "search_documents" and OUTAGE_SENTINEL in str(t.get("output") or "")
            for t in self.tools
        )

    def as_judge_input(self, numbers: Optional[list[dict]] = None) -> str:
        parts = [f"## Farmer's message\n{self.query or '(empty)'}"]
        if self.tools:
            parts.append("## Tool outputs this turn")
            for t in self.tools:
                out = str(t.get("output") or "(no output)")
                if len(out) > MAX_TOOL_OUTPUT_CHARS:
                    out = out[:MAX_TOOL_OUTPUT_CHARS] + "\n...[truncated]"
                parts.append(f"### {t['name']}\ninput: {json.dumps(t.get('input'), ensure_ascii=False, default=str)}\noutput:\n{out}")
        else:
            parts.append("## Tool outputs this turn\n(none)")
        parts.append(f"## Assistant's final response\n{self.response or '(empty)'}")
        if numbers:
            parts.append(
                "## Numbers spoken in the response (blind pre-conversion; trust these digits)\n"
                + "\n".join(
                    f'- "{n.get("spoken")}" = {n.get("digits")} ({n.get("refers_to", "?")})'
                    for n in numbers
                )
            )
        return "\n\n".join(parts)


def _extract_json(text: str) -> Optional[dict]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


class Judge:
    def __init__(self) -> None:
        from openai import OpenAI

        # gpt-4o-mini is not reliable here: it misparses Marathi spoken
        # numerals and drifts from the rubric on multi-row tool outputs.
        self.model = os.getenv("JUDGE_MODEL", "gpt-4o")
        api_key = os.getenv("JUDGE_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            sys.exit("Set JUDGE_API_KEY or OPENAI_API_KEY for the judge model.")
        self.client = OpenAI(base_url=os.getenv("JUDGE_BASE_URL") or None, api_key=api_key)

    def extract_numbers(self, response_text: str) -> list[dict]:
        """Blind pass: convert spoken numbers to digits WITHOUT showing the tool
        outputs. A judge that sees the reference while converting back-fits the
        conversion to it and digit transpositions slip through (observed with
        gpt-4o on a pincode)."""
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": NUMBER_EXTRACTION_SYSTEM},
                {"role": "user", "content": response_text},
            ],
        )
        parsed = _extract_json(resp.choices[0].message.content or "") or {}
        nums = parsed.get("numbers") or []
        return [n for n in nums if isinstance(n, dict) and n.get("digits")]

    def verdict(self, system: str, turn: TurnEvidence, numbers: Optional[list[dict]] = None) -> Optional[dict]:
        """Return {"verdict": "pass"|"fail", "evidence": ..., "reasoning": ...} or None."""
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": turn.as_judge_input(numbers)},
            ],
        )
        parsed = _extract_json(resp.choices[0].message.content or "")
        if not parsed or parsed.get("verdict") not in ("pass", "fail"):
            return None
        return parsed


def _get_langfuse():
    from langfuse import Langfuse

    public = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret = os.getenv("LANGFUSE_SECRET_KEY")
    if not public or not secret:
        sys.exit("Set LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY (see .env).")
    return Langfuse(
        public_key=public,
        secret_key=secret,
        base_url=os.getenv("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com",
    )


def _collect_evidence(lf, trace_id: str) -> Optional[TurnEvidence]:
    trace = lf.api.trace.get(trace_id)

    # Query/response live on the root agent observation (traced_voice_request
    # sets them there); trace-level input/output may mirror them, so fall back.
    root = next(
        (o for o in trace.observations if (o.type or "").upper() == "AGENT"),
        None,
    )
    query = trace.input or (root.input if root else None)
    response = trace.output or (root.output if root else None)
    if isinstance(query, dict):
        query = json.dumps(query, ensure_ascii=False)
    if isinstance(response, dict):
        response = json.dumps(response, ensure_ascii=False)
    if not response:
        return None  # nothing to judge (failed/empty turn)

    tools = [
        {"name": o.name, "input": o.input, "output": o.output}
        for o in sorted(trace.observations, key=lambda o: o.start_time or datetime.min.replace(tzinfo=timezone.utc))
        if (o.type or "").upper() == "TOOL"
    ]
    return TurnEvidence(
        trace_id=trace.id,
        timestamp=str(trace.timestamp),
        query=str(query or ""),
        response=str(response),
        tools=tools,
        existing_scores={s.name for s in (trace.scores or []) if getattr(s, "name", None)},
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24, help="How far back to fetch traces (default 24)")
    ap.add_argument("--limit", type=int, default=50, help="Max traces to judge (default 50)")
    ap.add_argument("--trace-id", action="append", default=[], help="Judge specific trace id(s) instead of a time window")
    ap.add_argument("--trace-name", default="voice", help="Langfuse trace name to fetch (default: voice)")
    ap.add_argument("--dry-run", action="store_true", help="Print verdicts, do not write scores")
    ap.add_argument("--rescore", action="store_true", help="Judge traces even if they already have these scores")
    args = ap.parse_args()

    lf = _get_langfuse()
    judge = Judge()

    if args.trace_id:
        trace_ids = args.trace_id
    else:
        since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        trace_ids, page = [], 1
        while len(trace_ids) < args.limit:
            batch = lf.api.trace.list(
                name=args.trace_name, from_timestamp=since, page=page, limit=min(50, args.limit)
            )
            trace_ids.extend(t.id for t in batch.data)
            if page >= batch.meta.total_pages:
                break
            page += 1
        trace_ids = trace_ids[: args.limit]

    if not trace_ids:
        print("No traces found.")
        return

    metrics = {
        "faithfulness": {"pass": 0, "fail": 0, "na": 0, "error": 0},
        "search_outage_honesty": {"pass": 0, "fail": 0, "na": 0, "error": 0},
    }

    for trace_id in trace_ids:
        try:
            turn = _collect_evidence(lf, trace_id)
        except Exception as e:
            print(f"[{trace_id}] fetch failed: {e}")
            continue
        if turn is None:
            for m in metrics.values():
                m["na"] += 1
            continue

        # (metric name, judge system prompt, applicability)
        plan = [
            ("faithfulness", FAITHFULNESS_SYSTEM, bool(turn.tools)),
            ("search_outage_honesty", OUTAGE_SYSTEM, turn.had_search_outage),
        ]

        numbers: list[dict] = []
        if any(applicable for _, _, applicable in plan):
            try:
                numbers = judge.extract_numbers(turn.response)
            except Exception as e:
                print(f"[{trace_id}] number extraction failed (judging without): {e}")

        for name, system, applicable in plan:
            if not applicable:
                metrics[name]["na"] += 1
                continue
            if name in turn.existing_scores and not args.rescore:
                continue
            try:
                result = judge.verdict(system, turn, numbers)
            except Exception as e:
                print(f"[{trace_id}] {name}: judge call failed: {e}")
                metrics[name]["error"] += 1
                continue
            if result is None:
                print(f"[{trace_id}] {name}: unparseable judge output")
                metrics[name]["error"] += 1
                continue

            verdict = result["verdict"]
            metrics[name][verdict] += 1
            comment = f"{result.get('reasoning', '')}\nEvidence: {result.get('evidence', '')}".strip()
            marker = "PASS" if verdict == "pass" else "FAIL"
            print(f"[{trace_id}] {name}: {marker} — {result.get('reasoning', '')}")

            if not args.dry_run:
                lf.create_score(
                    trace_id=trace_id,
                    name=name,
                    value=1.0 if verdict == "pass" else 0.0,
                    data_type="BOOLEAN",
                    comment=comment[:1000],
                )

    if not args.dry_run:
        lf.flush()

    print("\n=== Summary ===")
    for name, m in metrics.items():
        judged = m["pass"] + m["fail"]
        rate = f"{m['pass'] / judged:.0%}" if judged else "—"
        print(
            f"{name:24s} pass={m['pass']} fail={m['fail']} "
            f"(pass rate {rate})  n/a={m['na']} errors={m['error']}"
        )
    if args.dry_run:
        print("(dry run: no scores written)")


if __name__ == "__main__":
    main()
