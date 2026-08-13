"""Shared config and helpers for the prompt A/B benchmark."""
from __future__ import annotations

import os
import re
from pathlib import Path

import openpyxl

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = Path(__file__).resolve().parent
RUNS_DIR = BENCH_DIR / "runs"

QUESTIONS_XLSX = Path(
    os.getenv(
        "BENCH_QUESTIONS",
        Path.home() / "Downloads" / "mh-voice benchmarking questions .xlsx",
    )
)

# Arm A: current prompt, already deployed. Auth is not enforced on this route.
PROD_URL = os.getenv("BENCH_PROD_URL", "https://voice-prod.mahapocra.gov.in/api/voice/")
# Arm B: baseline prompt (cb529f2), served locally.
LOCAL_URL = os.getenv("BENCH_LOCAL_URL", "http://localhost:8010/api/voice/")

ARMS = {"prod": PROD_URL, "local": LOCAL_URL}

# PoCRA /search returns HTTP 200 with an empty responses[] at 3+ concurrent calls,
# and Marqo 429s above 5. Both arms share those backends, so keep this low and
# never run the two arms at the same time.
CONCURRENCY = int(os.getenv("BENCH_CONCURRENCY", "3"))
REQUEST_TIMEOUT = float(os.getenv("BENCH_TIMEOUT", "180"))


def load_env(path: Path | None = None) -> dict[str, str]:
    """Parse the repo .env without shell-ing out or mutating os.environ."""
    path = path or (REPO_ROOT / ".env")
    text = path.read_text(errors="replace")
    out: dict[str, str] = {}
    for key, value in re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", text, re.M):
        out[key] = value.strip().strip('"').strip("'")
    return out


def load_questions() -> list[dict]:
    """Read the benchmark question sheet into ordered dicts with stable ids."""
    workbook = openpyxl.load_workbook(QUESTIONS_XLSX)
    sheet = workbook["Sheet1"]
    rows = []
    for idx, (question, category, agristack) in enumerate(
        sheet.iter_rows(min_row=2, max_col=3, values_only=True)
    ):
        if not question:
            continue
        rows.append(
            {
                "qid": f"q{idx:03d}",
                "question": str(question).strip(),
                "category": str(category or "").strip(),
                "agristack_required": str(agristack or "").strip(),
            }
        )
    return rows


def word_count(text: str) -> int:
    return len(str(text).split())
