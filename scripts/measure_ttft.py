"""
Measure TTFT (time to first token) and streaming latency for the voice API.

Two modes, so you can tell APP overhead apart from MODEL prefill:

  api   — stream from the FastAPI voice endpoint (what the caller experiences).
          TTFT here = history load + profile load + agent prefill (+ tool
          rounds, if the query triggers tools) + first token.

  vllm  — hit the vLLM OpenAI endpoint directly with the real system prompt,
          no tools. TTFT here ≈ pure prefill + first token. Run it twice in a
          row with --runs 2: if run 2 is much faster than run 1, prefix
          caching is ON and working; if both are equally slow, it's off.

Usage:
  # end-to-end, 3 runs, fresh session each run (worst case: no session cache)
  python scripts/measure_ttft.py api --query "कापसाचा भाव काय आहे?" --runs 3

  # same session across runs (turn 2+ latency, history in Redis)
  python scripts/measure_ttft.py api --query "नमस्कार" --runs 3 --same-session

  # raw model prefill with the actual Marathi system prompt
  python scripts/measure_ttft.py vllm --runs 3

Env: VOICE_API_URL (default http://localhost:8003/api/voice/),
     VLLM_OPENAI_BASE_URL / VLLM_AGRINET_MODEL_URL, LLM_AGRINET_MODEL_NAME,
     INFERENCE_API_KEY, VOICE_API_TOKEN (Bearer for production API).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))
    return s[idx]


def _summarize(label: str, ttfts: list[float], totals: list[float]) -> None:
    print(f"\n=== {label} summary over {len(ttfts)} run(s) ===")
    for name, vals in (("TTFT", ttfts), ("total", totals)):
        if not vals:
            continue
        print(
            f"{name:>6}: min={min(vals):.0f}ms  median={statistics.median(vals):.0f}ms  "
            f"p95={_percentile(vals, 95):.0f}ms  max={max(vals):.0f}ms"
        )


def run_api(args: argparse.Namespace) -> None:
    url = args.url or os.getenv("VOICE_API_URL", "http://localhost:8003/api/voice/")
    token = os.getenv("VOICE_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    session_id = args.session_id or f"ttft-{uuid.uuid4().hex[:8]}"

    ttfts: list[float] = []
    totals: list[float] = []

    with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        for i in range(args.runs):
            sid = session_id if args.same_session else f"ttft-{uuid.uuid4().hex[:8]}"
            params = {
                "query": args.query,
                "session_id": sid,
                "source_lang": args.lang,
                "target_lang": args.lang,
                "user_id": args.user_id,
            }
            t0 = time.perf_counter()
            first_chunk_ms: float | None = None
            chunk_times: list[float] = []
            chars = 0
            preview = ""
            with client.stream("GET", url, params=params, headers=headers) as resp:
                resp.raise_for_status()
                last = t0
                for chunk in resp.iter_text():
                    now = time.perf_counter()
                    if first_chunk_ms is None:
                        first_chunk_ms = (now - t0) * 1000
                    else:
                        chunk_times.append((now - last) * 1000)
                    last = now
                    chars += len(chunk)
                    if len(preview) < 80:
                        preview += chunk
            total_ms = (time.perf_counter() - t0) * 1000
            ttfts.append(first_chunk_ms or total_ms)
            totals.append(total_ms)
            gaps = f"avg_gap={statistics.mean(chunk_times):.0f}ms max_gap={max(chunk_times):.0f}ms" if chunk_times else "single-chunk!"
            print(
                f"run {i + 1}: session={sid} TTFT={first_chunk_ms:.0f}ms total={total_ms:.0f}ms "
                f"chars={chars} chunks={len(chunk_times) + 1} {gaps}"
            )
            print(f"        preview: {preview[:80]!r}")
            if len(chunk_times) == 0 and chars > 50:
                print("        WARNING: whole response arrived in ONE chunk — something "
                      "between you and uvicorn is buffering (proxy/client).")
    _summarize("API", ttfts, totals)


def run_convo(args: argparse.Namespace) -> None:
    """Multi-turn conversation in ONE fresh session: per-turn TTFT + full replies,
    so you can judge both latency and whether context carries across turns."""
    url = args.url or os.getenv("VOICE_API_URL", "http://localhost:8003/api/voice/")
    token = os.getenv("VOICE_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    session_id = args.session_id or f"convo-{uuid.uuid4().hex[:8]}"
    print(f"session={session_id} user_id={args.user_id}\n")

    ttfts: list[float] = []
    totals: list[float] = []
    with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
        for i, query in enumerate(args.queries):
            params = {
                "query": query,
                "session_id": session_id,
                "source_lang": args.lang,
                "target_lang": args.lang,
                "user_id": args.user_id,
            }
            t0 = time.perf_counter()
            first_chunk_ms: float | None = None
            text = ""
            with client.stream("GET", url, params=params, headers=headers) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_text():
                    if first_chunk_ms is None:
                        first_chunk_ms = (time.perf_counter() - t0) * 1000
                    text += chunk
            total_ms = (time.perf_counter() - t0) * 1000
            ttfts.append(first_chunk_ms or total_ms)
            totals.append(total_ms)
            print(f"--- turn {i + 1} ---")
            print(f"USER: {query}")
            print(f"BOT ({first_chunk_ms:.0f}ms TTFT, {total_ms:.0f}ms total, {len(text)} chars):")
            print(f"{text}\n")
    _summarize("conversation", ttfts, totals)


def _vllm_base_url() -> str | None:
    override = os.getenv("VLLM_OPENAI_BASE_URL")
    if override:
        u = override.strip().rstrip("/")
        return u if u.endswith("/v1") else f"{u}/v1"
    listed = os.getenv("VLLM_AGRINET_MODEL_URL")
    if listed:
        u = listed.strip().rstrip("/")
        for suffix in ("/v1/models", "/models"):
            if u.endswith(suffix):
                u = u[: -len("/models")]
                break
        return u if u.endswith("/v1") else f"{u}/v1"
    u = os.getenv("INFERENCE_ENDPOINT_URL", "").strip().rstrip("/")
    if not u:
        return None
    return u if u.endswith("/v1") else f"{u}/v1"


def run_vllm(args: argparse.Namespace) -> None:
    base_url = args.url or _vllm_base_url()
    if not base_url:
        sys.exit("Set VLLM_OPENAI_BASE_URL (or VLLM_AGRINET_MODEL_URL / INFERENCE_ENDPOINT_URL).")
    model = args.model or os.getenv("LLM_AGRINET_MODEL_NAME") or os.getenv("LLM_MODEL_NAME")
    if not model:
        sys.exit("Set LLM_AGRINET_MODEL_NAME (or pass --model).")
    api_key = os.getenv("INFERENCE_API_KEY") or "not-required"

    prompt_path = REPO_ROOT / "assets" / "prompts" / f"voice_system_{args.lang}.md"
    system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else "You are a helpful assistant."
    print(f"model={model} base_url={base_url}")
    print(f"system prompt: {prompt_path.name} ({len(system_prompt)} chars)")

    body = {
        "model": model,
        "stream": True,
        "temperature": 0.7,
        "max_tokens": 128,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": args.query},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    ttfts: list[float] = []
    totals: list[float] = []
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        for i in range(args.runs):
            t0 = time.perf_counter()
            first_ms: float | None = None
            n_tokens = 0
            usage = {}
            with client.stream("POST", f"{base_url}/chat/completions", json=body, headers=headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if event.get("usage"):
                        usage = event["usage"]
                    choices = event.get("choices") or []
                    if choices and (choices[0].get("delta") or {}).get("content"):
                        if first_ms is None:
                            first_ms = (time.perf_counter() - t0) * 1000
                        n_tokens += 1
            total_ms = (time.perf_counter() - t0) * 1000
            ttfts.append(first_ms or total_ms)
            totals.append(total_ms)
            decode = ""
            if first_ms is not None and n_tokens > 1:
                decode = f" decode={(n_tokens - 1) / ((total_ms - first_ms) / 1000):.0f} tok/s"
            print(f"run {i + 1}: TTFT={first_ms:.0f}ms total={total_ms:.0f}ms chunks={n_tokens}{decode} "
                  f"prompt_tokens={usage.get('prompt_tokens', '?')}")
    _summarize("vLLM", ttfts, totals)
    if len(ttfts) >= 2 and ttfts[0] > 0:
        ratio = ttfts[1] / ttfts[0]
        if ratio < 0.5:
            print(f"\nrun2/run1 TTFT = {ratio:.2f} → prefix caching looks ENABLED (repeat prefill is cheap).")
        else:
            print(f"\nrun2/run1 TTFT = {ratio:.2f} → repeat prefill is NOT cheaper: prefix caching "
                  f"likely DISABLED on the vLLM server (start it with --enable-prefix-caching).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)

    p_api = sub.add_parser("api", help="measure the FastAPI voice endpoint end-to-end")
    p_api.add_argument("--url", default=None)
    p_api.add_argument("--query", default="नमस्कार")
    p_api.add_argument("--lang", default="mr")
    p_api.add_argument("--user-id", default="9999999999", help="dev-mode user_id (phone) for memory")
    p_api.add_argument("--session-id", default=None)
    p_api.add_argument("--same-session", action="store_true", help="reuse one session across runs (turn 2+ latency)")
    p_api.add_argument("--runs", type=int, default=3)

    p_convo = sub.add_parser("convo", help="multi-turn conversation in one session (context + per-turn TTFT)")
    p_convo.add_argument("queries", nargs="+", help="queries sent in order within one session")
    p_convo.add_argument("--url", default=None)
    p_convo.add_argument("--lang", default="mr")
    p_convo.add_argument("--user-id", default="9999999999")
    p_convo.add_argument("--session-id", default=None)

    p_vllm = sub.add_parser("vllm", help="measure raw model TTFT with the real system prompt (no tools)")
    p_vllm.add_argument("--url", default=None, help="OpenAI-compatible base URL ending in /v1")
    p_vllm.add_argument("--model", default=None)
    p_vllm.add_argument("--query", default="कापसाचा आजचा भाव काय आहे?")
    p_vllm.add_argument("--lang", default="mr", choices=["mr", "hi", "en"])
    p_vllm.add_argument("--runs", type=int, default=3)

    args = parser.parse_args()
    if args.mode == "api":
        run_api(args)
    elif args.mode == "convo":
        run_convo(args)
    else:
        run_vllm(args)


if __name__ == "__main__":
    main()
