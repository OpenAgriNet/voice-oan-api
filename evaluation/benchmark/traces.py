"""Join Langfuse traces onto benchmark runs to recover tool calls and tool outputs.

The judge needs what the tools actually returned, otherwise `no_fabrication` and
`accuracy_completeness` are unfalsifiable. Traces are looked up by the session_id
the runner minted per question.

    python evaluation/benchmark/traces.py runs/raw_<id>_<arm>.jsonl
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

import httpx

from common import RUNS_DIR, load_env

MAX_TOOL_OUTPUT_CHARS = 4000


def client() -> tuple[httpx.Client, str]:
    env = load_env()
    base = env["LANGFUSE_BASE_URL"].rstrip("/")
    token = base64.b64encode(
        f"{env['LANGFUSE_PUBLIC_KEY']}:{env['LANGFUSE_SECRET_KEY']}".encode()
    ).decode()
    return (
        httpx.Client(timeout=60.0, headers={"Authorization": f"Basic {token}"}),
        base,
    )


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def fetch_trace(http: httpx.Client, base: str, session_id: str) -> dict | None:
    """Find the trace for a session and pull its observations."""
    listing = http.get(f"{base}/api/public/traces", params={"sessionId": session_id, "limit": 5})
    listing.raise_for_status()
    data = listing.json().get("data") or []
    if not data:
        return None

    trace_id = data[0]["id"]
    detail = http.get(f"{base}/api/public/traces/{trace_id}")
    detail.raise_for_status()
    trace = detail.json()

    tool_calls = []
    for obs in trace.get("observations", []):
        if (obs.get("type") or "").upper() != "TOOL":
            continue
        kwargs = {}
        raw_input = obs.get("input")
        if isinstance(raw_input, dict):
            kwargs = raw_input.get("kwargs") or {}
        tool_calls.append(
            {
                "name": obs.get("name"),
                "args": kwargs,
                "output": _as_text(obs.get("output"))[:MAX_TOOL_OUTPUT_CHARS],
            }
        )

    return {
        "trace_id": trace_id,
        "trace_env": data[0].get("environment"),
        "trace_tags": data[0].get("tags") or [],
        "tool_calls": tool_calls,
        "tool_names": [t["name"] for t in tool_calls],
        "n_tool_calls": len(tool_calls),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("raw_file")
    parser.add_argument("--settle", type=int, default=20,
                        help="seconds to wait for Langfuse ingestion before starting")
    args = parser.parse_args()

    raw_path = Path(args.raw_file)
    if not raw_path.is_absolute():
        raw_path = RUNS_DIR / raw_path.name
    records = [json.loads(line) for line in raw_path.read_text().splitlines() if line.strip()]

    if args.settle:
        print(f"waiting {args.settle}s for Langfuse ingestion...", flush=True)
        time.sleep(args.settle)

    http, base = client()
    missing = 0
    with http:
        for i, record in enumerate(records, 1):
            try:
                trace = fetch_trace(http, base, record["session_id"])
            except Exception as exc:  # noqa: BLE001
                record["trace_error"] = f"{type(exc).__name__}: {exc}"
                trace = None
            if trace is None:
                missing += 1
                record.setdefault("trace_error", "trace_not_found")
                record["tool_calls"] = []
                record["tool_names"] = []
                record["n_tool_calls"] = 0
            else:
                record.update(trace)
            if i % 25 == 0 or i == len(records):
                print(f"  {i}/{len(records)} joined, {missing} missing", flush=True)

    out_path = raw_path.with_name(raw_path.name.replace("raw_", "joined_"))
    with out_path.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    no_tools = sum(1 for r in records if not r.get("n_tool_calls"))
    print(f"\nwrote {out_path}")
    print(f"  traces missing : {missing}/{len(records)}")
    print(f"  zero tool calls: {no_tools}/{len(records)}")
    if missing == len(records):
        sys.exit("every trace lookup failed - check Langfuse credentials or ingestion lag")


if __name__ == "__main__":
    main()
