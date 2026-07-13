"""Burst factuality regression eval for the voice agent.

Replays the 2026-07-06 burst test: N concurrent, identical Marathi queries
(each with a unique session ID) against the /voice/ endpoint, then judges every
response for factual correctness with an LLM judge pinned to temperature 0.

The judge receives a fixed reference list of Maharashtra Rabi (winter) crops and
flags responses that recommend non-winter crops, hallucinated/invalid crop
names, or malformed Marathi tokens.

Usage:
    python -m evaluation.burst_factuality_eval --url http://localhost:8000/voice/ --n 100

Exits non-zero if the observed factual error rate exceeds --threshold (default
5%), so it can gate CI or pre-deploy checks. Writes a per-response JSON report
next to this file (or to --output).

Requires the same Azure OpenAI env vars as the app (AZURE_OPENAI_ENDPOINT,
AZURE_OPENAI_API_KEY, AZURE_OPENAI_API_VERSION, AZURE_OPENAI_DEPLOYMENT_NAME).
"""

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI

load_dotenv()

DEFAULT_QUERY = "हिवाळी पिके कोणती आहेत?"

# Reference list for the judge. Crops commonly accepted as Rabi (winter) season
# crops in Maharashtra. The judge treats this as guidance, not an exhaustive
# whitelist — regionally valid Rabi crops outside this list are acceptable.
RABI_CROPS_REFERENCE = [
    "गहू (wheat)",
    "हरभरा (chickpea/gram)",
    "रब्बी ज्वारी (rabi sorghum/jowar)",
    "करडई (safflower)",
    "मोहरी (mustard)",
    "जवस (linseed)",
    "सूर्यफूल (sunflower, rabi)",
    "मका (maize, rabi)",
    "कांदा (onion, rabi)",
    "लसूण (garlic)",
    "वाटाणा (pea)",
    "बटाटा (potato)",
]

JUDGE_SYSTEM_PROMPT = f"""You are evaluating responses from a Marathi agricultural voice assistant for farmers in Maharashtra.

The user asked: "{DEFAULT_QUERY}" (Which crops are grown in the winter/Rabi season?)

Reference list of accepted Rabi (winter) crops for Maharashtra:
{chr(10).join('- ' + c for c in RABI_CROPS_REFERENCE)}

Judge the response on these criteria:
1. Crops named must be real crops with correctly spelled Marathi names. Nonsense words, malformed tokens, or non-crop items (e.g. "बेडूक"/frog, "ड्रॉग") are automatic failures.
2. Crops recommended as winter crops must actually be Rabi-season crops. Kharif/monsoon crops (cotton/कापूस, soybean/सोयाबीन) or perennial/annual crops presented as winter crops (sugarcane/ऊस) are failures. Fruit crops like apple that are not grown in Maharashtra's winter cropping system are failures.
3. Crops outside the reference list are acceptable if they are genuinely grown in the Rabi season in Maharashtra.
4. The response may include follow-up questions, greetings, or extra guidance — judge only the factual crop content.
5. A response that asks a clarifying question or declines without naming wrong crops counts as "acceptable".

Respond with ONLY a JSON object:
{{"verdict": "correct" | "acceptable" | "incorrect", "problems": ["<each factual problem found, in English>"]}}

"correct": lists valid Rabi crops with no factual errors.
"acceptable": no factual errors, but incomplete or a clarifying question.
"incorrect": any hallucinated/invalid token, non-crop item, or non-Rabi crop presented as a winter crop.
"""


async def fire_request(
    client: httpx.AsyncClient, url: str, query: str, index: int
) -> dict:
    """Send one voice request with a unique session ID and collect the streamed text."""
    session_id = f"eval-burst-{uuid.uuid4()}"
    started = time.monotonic()
    try:
        response_text = ""
        async with client.stream(
            "GET",
            url,
            params={
                "query": query,
                "session_id": session_id,
                "source_lang": "mr",
                "target_lang": "mr",
                "user_id": "burst-eval",
            },
        ) as resp:
            status = resp.status_code
            async for chunk in resp.aiter_text():
                response_text += chunk
        return {
            "index": index,
            "session_id": session_id,
            "status": status,
            "latency_s": round(time.monotonic() - started, 2),
            "response": response_text.strip(),
            "error": None,
        }
    except Exception as exc:
        return {
            "index": index,
            "session_id": session_id,
            "status": None,
            "latency_s": round(time.monotonic() - started, 2),
            "response": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


async def judge_response(
    judge_client: AsyncAzureOpenAI,
    deployment: str,
    result: dict,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Attach a factuality verdict to one request result."""
    if result["error"] or result["status"] != 200 or not result["response"]:
        result["verdict"] = "request_failed"
        result["problems"] = [result["error"] or f"HTTP {result['status']}: {result['response'][:200] or 'empty response'}"]
        return result

    async with semaphore:
        try:
            completion = await judge_client.chat.completions.create(
                model=deployment,
                temperature=0,
                seed=42,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Response to evaluate:\n\n{result['response']}"},
                ],
            )
            verdict = json.loads(completion.choices[0].message.content)
            result["verdict"] = verdict.get("verdict", "judge_failed")
            result["problems"] = verdict.get("problems", [])
        except Exception as exc:
            result["verdict"] = "judge_failed"
            result["problems"] = [f"{type(exc).__name__}: {exc}"]
    return result


async def run_eval(args: argparse.Namespace) -> int:
    judge_client = AsyncAzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/"),
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
    )
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"]

    print(f"Firing {args.n} concurrent requests at {args.url} ...")
    limits = httpx.Limits(max_connections=args.n)
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        results = await asyncio.gather(
            *[fire_request(client, args.url, args.query, i) for i in range(args.n)]
        )

    ok = sum(1 for r in results if r["status"] == 200)
    print(f"Requests complete: {ok}/{args.n} returned HTTP 200. Judging responses ...")

    semaphore = asyncio.Semaphore(args.judge_concurrency)
    results = list(
        await asyncio.gather(
            *[judge_response(judge_client, deployment, r, semaphore) for r in results]
        )
    )

    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    judged = sum(counts.get(v, 0) for v in ("correct", "acceptable", "incorrect"))
    incorrect = counts.get("incorrect", 0)
    error_rate = incorrect / judged if judged else 1.0

    report = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "query": args.query,
        "total_requests": args.n,
        "http_200": ok,
        "verdict_counts": counts,
        "factual_error_rate": round(error_rate, 4),
        "threshold": args.threshold,
        "passed": error_rate <= args.threshold,
        "results": results,
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print()
    print(f"Verdicts: {counts}")
    print(f"Factual error rate: {error_rate:.1%} (threshold {args.threshold:.1%})")
    print(f"Report written to {output}")

    if counts.get("judge_failed"):
        print(f"WARNING: judge failed on {counts['judge_failed']} responses; they are excluded from the error rate.")

    if error_rate > args.threshold:
        print("FAIL: factual error rate exceeds threshold.")
        incorrect_samples = [r for r in results if r["verdict"] == "incorrect"][:5]
        for r in incorrect_samples:
            print(f"  [{r['index']}] {r['problems']}")
        return 1
    print("PASS")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default="http://localhost:8000/api/voice/", help="Voice endpoint URL")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Query to send (default: winter crops, Marathi)")
    parser.add_argument("--n", type=int, default=100, help="Number of concurrent requests")
    parser.add_argument("--threshold", type=float, default=0.05, help="Max acceptable factual error rate (0-1)")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-request timeout in seconds")
    parser.add_argument("--judge-concurrency", type=int, default=10, help="Max concurrent judge calls")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).parent / "burst_factuality_report.json"),
        help="Path for the JSON report",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run_eval(args)))


if __name__ == "__main__":
    main()
