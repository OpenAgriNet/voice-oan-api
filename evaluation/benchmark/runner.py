"""Drive the voice endpoint over the benchmark question set and record answers.

Each (arm, pass, question) gets its own session_id so no run inherits another's
Redis history, and so the Langfuse trace can be joined back afterwards.

    python evaluation/benchmark/runner.py --arm prod --passes 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone

import httpx

from common import (
    ARMS,
    CONCURRENCY,
    REQUEST_TIMEOUT,
    RUNS_DIR,
    load_questions,
    word_count,
)


async def ask_one(
    client: httpx.AsyncClient,
    url: str,
    row: dict,
    session_id: str,
    semaphore: asyncio.Semaphore,
) -> dict:
    """One question, one fresh session. Never raises; failures land in `error`."""
    params = {
        "query": row["question"],
        "session_id": session_id,
        "source_lang": "mr",
        "target_lang": "mr",
    }
    record = {
        **row,
        "session_id": session_id,
        "answer": "",
        "elapsed_seconds": None,
        "ttfb_seconds": None,
        "word_count": 0,
        "error": "",
    }

    async with semaphore:
        started = time.perf_counter()
        first_chunk_at = None
        chunks: list[str] = []
        try:
            async with client.stream("GET", url, params=params) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode(errors="replace")
                    record["error"] = f"HTTP {response.status_code}: {body[:200]}"
                    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
                    return record
                async for chunk in response.aiter_text():
                    if not chunk:
                        continue
                    if first_chunk_at is None:
                        first_chunk_at = time.perf_counter()
                    chunks.append(chunk)
        except Exception as exc:  # noqa: BLE001 - a failed call is data, not a crash
            record["error"] = f"{type(exc).__name__}: {exc}"

        answer = "".join(chunks).strip()
        record["answer"] = answer
        record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        if first_chunk_at is not None:
            record["ttfb_seconds"] = round(first_chunk_at - started, 3)
        record["word_count"] = word_count(answer)
        if not answer and not record["error"]:
            record["error"] = "empty_response"
    return record


async def run_pass(arm: str, url: str, rows: list[dict], pass_n: int, run_id: str) -> list[dict]:
    semaphore = asyncio.Semaphore(CONCURRENCY)
    limits = httpx.Limits(max_connections=CONCURRENCY + 2)
    timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=30.0)
    results: list[dict] = []

    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
        tasks = [
            ask_one(
                client,
                url,
                row,
                f"bench-{run_id}-{arm}-p{pass_n}-{row['qid']}",
                semaphore,
            )
            for row in rows
        ]
        done = 0
        for coro in asyncio.as_completed(tasks):
            record = await coro
            record["arm"] = arm
            record["pass"] = pass_n
            record["run_id"] = run_id
            results.append(record)
            done += 1
            if done % 10 == 0 or done == len(tasks):
                errors = sum(1 for r in results if r["error"])
                print(f"  [{arm} pass {pass_n}] {done}/{len(tasks)} done, {errors} errors", flush=True)

    results.sort(key=lambda r: r["qid"])
    return results


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", required=True, choices=sorted(ARMS))
    parser.add_argument("--passes", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0, help="only first N questions (smoke)")
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%m%d%H%M"))
    args = parser.parse_args()

    rows = load_questions()
    if args.limit:
        rows = rows[: args.limit]

    url = ARMS[args.arm]
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"arm={args.arm} url={url} questions={len(rows)} passes={args.passes} "
          f"concurrency={CONCURRENCY} run_id={args.run_id}", flush=True)

    all_results: list[dict] = []
    for pass_n in range(1, args.passes + 1):
        started = time.perf_counter()
        all_results.extend(await run_pass(args.arm, url, rows, pass_n, args.run_id))
        print(f"  pass {pass_n} took {time.perf_counter() - started:.0f}s", flush=True)

    out_path = RUNS_DIR / f"raw_{args.run_id}_{args.arm}.jsonl"
    with out_path.open("w") as handle:
        for record in all_results:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    ok = [r for r in all_results if not r["error"]]
    latencies = sorted(r["elapsed_seconds"] for r in ok if r["elapsed_seconds"])
    print(f"\nwrote {out_path}")
    print(f"  {len(ok)}/{len(all_results)} succeeded")
    if latencies:
        print(f"  latency p50={latencies[len(latencies)//2]:.1f}s "
              f"p95={latencies[int(len(latencies)*0.95)]:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
