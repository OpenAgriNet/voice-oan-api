"""LLM-as-judge scoring over joined benchmark runs.

Runs on the same Azure gpt-4.1 deployment that serves the assistant, because that
is the only credential in .env. Judge and subject therefore share a model family,
so rubrics are kept mechanical and grounded in the trace tool output. Judge
temperature is pinned to 0 with a fixed seed so scores are reproducible even
though the system under test runs at temperature 1.0.

    python evaluation/benchmark/judge.py runs/joined_<id>_<arm>.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from openai import AsyncAzureOpenAI

from common import RUNS_DIR, load_env
from rubric import METRICS, build_system_prompt, build_user_prompt

JUDGE_CONCURRENCY = 3  # shared Azure deployment with live prod traffic
MAX_ATTEMPTS = 3


def make_client() -> tuple[AsyncAzureOpenAI, str]:
    env = load_env()
    client = AsyncAzureOpenAI(
        azure_endpoint=env["AZURE_OPENAI_ENDPOINT"].rstrip("/"),
        api_key=env["AZURE_OPENAI_API_KEY"],
        api_version=env["AZURE_OPENAI_API_VERSION"],
    )
    return client, env["AZURE_OPENAI_DEPLOYMENT_NAME"]


def _coerce(payload: dict) -> dict:
    """Clamp scores into range and guarantee every rubric key exists."""
    out: dict[str, object] = {}
    for metric, (max_score, _) in METRICS.items():
        raw = payload.get(f"score_{metric}")
        try:
            score = int(round(float(raw)))
        except (TypeError, ValueError):
            score = None
        if score is not None:
            score = max(0, min(max_score, score))
        out[f"score_{metric}"] = score
        reason = payload.get(f"reason_{metric}") or ""
        out[f"reason_{metric}"] = str(reason).replace("\n", " ").strip()[:400]
    return out


async def judge_one(
    client: AsyncAzureOpenAI,
    deployment: str,
    system_prompt: str,
    record: dict,
    semaphore: asyncio.Semaphore,
) -> dict:
    async with semaphore:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.chat.completions.create(
                    model=deployment,
                    temperature=0,
                    seed=42,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": build_user_prompt(record)},
                    ],
                )
                payload = json.loads(response.choices[0].message.content)
                scored = _coerce(payload)
                scored["judge_error"] = ""
                return {**record, **scored}
            except Exception as exc:  # noqa: BLE001
                if attempt == MAX_ATTEMPTS:
                    blank = {f"score_{m}": None for m in METRICS}
                    blank.update({f"reason_{m}": "" for m in METRICS})
                    blank["judge_error"] = f"{type(exc).__name__}: {exc}"[:300]
                    return {**record, **blank}
                await asyncio.sleep(2 * attempt)
    raise AssertionError("unreachable")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("joined_file")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    path = Path(args.joined_file)
    if not path.is_absolute():
        path = RUNS_DIR / path.name
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if args.limit:
        records = records[: args.limit]

    client, deployment = make_client()
    system_prompt = build_system_prompt()
    semaphore = asyncio.Semaphore(JUDGE_CONCURRENCY)

    print(f"judging {len(records)} records with {deployment} "
          f"(concurrency {JUDGE_CONCURRENCY})", flush=True)

    scored: list[dict] = []
    async with client:
        tasks = [judge_one(client, deployment, system_prompt, r, semaphore) for r in records]
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            scored.append(await coro)
            if i % 25 == 0 or i == len(tasks):
                failed = sum(1 for r in scored if r.get("judge_error"))
                print(f"  {i}/{len(tasks)} judged, {failed} judge errors", flush=True)

    scored.sort(key=lambda r: (r.get("pass", 0), r["qid"]))
    out_path = path.with_name(path.name.replace("joined_", "scored_"))
    with out_path.open("w") as handle:
        for record in scored:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
