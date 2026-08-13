"""Multi-turn probes for the three recurring issues a single-turn set cannot reach.

Covers: Context Retention Failure (6 field occurrences), Needs Repetition (7), and
Persists in Error After Correction (2). Each conversation replays sequentially on
one shared session_id so the Redis history accumulates like a real call.

    python evaluation/benchmark/multiturn.py --arm prod
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx

from common import ARMS, REQUEST_TIMEOUT, RUNS_DIR, word_count

# probe: what the graded (final) turn is testing.
#   context_retention  - the last turn depends on a fact established earlier
#   topic_switch       - the last turn changes crop/topic; earlier data must NOT leak
#   repetition         - the same question asked twice; turn 2 should not improve on turn 1
#   correction         - the farmer corrects the assistant; it must not repeat the error
CONVERSATIONS = [
    {
        "cid": "c01", "probe": "context_retention",
        "turns": [
            "नाशिक जिल्ह्यात उद्या पाऊस पडेल का?",
            "मग सोयाबीन कधी पेरू?",
        ],
        "expect": "Sowing advice must be consistent with the rain forecast it just gave.",
    },
    {
        "cid": "c02", "probe": "context_retention",
        "turns": [
            "अकोला जिल्ह्यात तुरीचा बाजारभाव काय आहे?",
            "मी तुम्हाला आधी काय विचारले होते?",
        ],
        "expect": "Must recall the earlier question about tur prices in Akola.",
    },
    {
        "cid": "c03", "probe": "topic_switch",
        "turns": [
            "गव्हाचा बाजारभाव काय आहे?",
            "कापसाला कोणते खत द्यावे?",
        ],
        "expect": "Cotton fertiliser answer must not reuse wheat price data; needs a fresh tool call.",
    },
    {
        "cid": "c04", "probe": "topic_switch",
        "turns": [
            "सोयाबीनवरील करपा रोगावर काय उपाय आहे?",
            "उसावर हुमणी अळीचे नियंत्रण कसे करावे?",
        ],
        "expect": "Sugarcane grub answer must not carry over the soybean disease advice.",
    },
    {
        "cid": "c05", "probe": "repetition",
        "turns": [
            "कोरडवाहू भागासाठी ज्वारीचे कोणते वाण योग्य आहे?",
            "कोरडवाहू भागासाठी ज्वारीचे कोणते वाण योग्य आहे?",
        ],
        "expect": "Turn 1 should already be complete; turn 2 should not be materially better.",
    },
    {
        "cid": "c06", "probe": "repetition",
        "turns": [
            "बुरशीनाशक फवारणी कधी करावी?",
            "बुरशीनाशक फवारणी कधी करावी?",
        ],
        "expect": "Turn 1 should already answer timing; repetition should not be required.",
    },
    {
        "cid": "c07", "probe": "correction",
        "turns": [
            "उसाला हुमणी अळी लागली आहे, काय करू?",
            "नाही, ती हुमणी नाही, पिठ्या ढेकूण आहे. आता काय करू?",
        ],
        "expect": "Must accept the mealybug correction and not repeat white-grub advice.",
    },
    {
        "cid": "c08", "probe": "correction",
        "turns": [
            "मनरेगा मजुरीचा दर किती आहे?",
            "तुम्ही सांगितलेला दर चुकीचा वाटतो, पुन्हा तपासा.",
        ],
        "expect": "Must re-check rather than restate the same figure with more confidence.",
    },
    {
        "cid": "c09", "probe": "context_retention",
        "turns": [
            "माझ्याकडे लातूरमध्ये दोन एकर जमीन आहे.",
            "मी कोणते पीक घ्यावे?",
        ],
        "expect": "Crop advice must use the Latur location and 2-acre size from turn 1.",
    },
    {
        "cid": "c10", "probe": "correction",
        "turns": [
            "टोमॅटो पिकावर कोणती फवारणी करावी?",
            "मी टोमॅटो नाही, मका म्हणालो होतो.",
        ],
        "expect": "Must switch to maize and call tools afresh, not restate tomato advice.",
    },
]


async def run_conversation(
    client: httpx.AsyncClient, url: str, convo: dict, arm: str, run_id: str
) -> list[dict]:
    """Replay one conversation's turns in order on a single shared session."""
    session_id = f"bench-{run_id}-{arm}-mt-{convo['cid']}"
    records = []
    for turn_no, question in enumerate(convo["turns"], 1):
        params = {
            "query": question,
            "session_id": session_id,
            "source_lang": "mr",
            "target_lang": "mr",
        }
        started = time.perf_counter()
        answer, error = "", ""
        try:
            async with client.stream("GET", url, params=params) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode(errors="replace")
                    error = f"HTTP {response.status_code}: {body[:200]}"
                else:
                    chunks = [c async for c in response.aiter_text()]
                    answer = "".join(chunks).strip()
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"

        records.append({
            "qid": f"{convo['cid']}t{turn_no}",
            "cid": convo["cid"],
            "probe": convo["probe"],
            "expect": convo["expect"],
            "turn": turn_no,
            "is_final_turn": turn_no == len(convo["turns"]),
            "category": f"multiturn:{convo['probe']}",
            "question": question,
            "answer": answer,
            "session_id": session_id,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "word_count": word_count(answer),
            "error": error,
            "arm": arm,
            "run_id": run_id,
            "pass": 1,
        })
    return records


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", required=True, choices=sorted(ARMS))
    parser.add_argument("--run-id", default="mt01")
    args = parser.parse_args()

    url = ARMS[args.arm]
    timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=30.0)
    results: list[dict] = []

    # Conversations run one at a time: turns are order-dependent, and the shared
    # PoCRA/Marqo backends throttle under concurrency anyway.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for i, convo in enumerate(CONVERSATIONS, 1):
            results.extend(await run_conversation(client, url, convo, args.arm, args.run_id))
            print(f"  {i}/{len(CONVERSATIONS)} conversations done", flush=True)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RUNS_DIR / f"raw_{args.run_id}_{args.arm}_multiturn.jsonl"
    with out_path.open("w") as handle:
        for record in results:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    errors = sum(1 for r in results if r["error"])
    print(f"\nwrote {out_path} ({len(results)} turns, {errors} errors)")


if __name__ == "__main__":
    asyncio.run(main())
