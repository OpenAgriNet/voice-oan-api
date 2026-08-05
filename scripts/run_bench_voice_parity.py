#!/usr/bin/env python3
"""VOICE parity bench runner: bare gemma4 + the two prod terminology gates.

Identical to scripts/run_bench_voice.py except for two added flags that port
production's Gujarati terminology enforcement -- which lives ENTIRELY in the
translation layer and is therefore bypassed by BARE_GU_NATIVE=1 -- onto the
bare agent:

  --inject-rules : appends the 33 GU_PREFERRED_TRANSLATION_RULES (imported
                   verbatim from app/services/translation.py) to the voice
                   agent's instructions AT RUNTIME, rephrased from
                   translator-instructions to agent-instructions. Every
                   Gujarati term is reproduced character-for-character.
                   assets/prompts/voice_system_gu_native.md is NOT touched.

  --apply-map    : applies assets/gu_term_policy.json "forbidden" pairs to the
                   answer post-hoc, longest-key-first (phrase beats word),
                   exactly like translation._build_gu_policy_replacements.

Output schema is the base runner's schema plus a trailing `answer_raw` column
holding the pre-map answer.

Must run with cwd = repo root; assets load via relative paths.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from pathlib import Path

csv.field_size_limit(sys.maxsize)

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env.experiment"
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH, override=True)
else:
    print(f"[warn] .env.experiment not found at {ENV_PATH}")

os.environ.setdefault("LOGFIRE_SEND_TO_LOGFIRE", "false")
os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")
os.environ.setdefault("LOGFIRE_TOKEN", "")

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from guided_patch import guided_settings  # noqa: E402

FIELDS = [
    "row_num", "qid", "arm", "guided", "question", "answer",
    "latency_s", "error", "num_tool_calls", "tool_names", "tool_log",
    "answer_raw",
]

# --------------------------------------------------------------------------
# Gate 1: the 33 rules, rephrased translator -> agent voice.
# Only the rules that literally speak about "translating" / "the English
# source" are reworded; the other 30 are used verbatim. Gujarati is untouched
# everywhere. Keys must match translation.py exactly -- we assert that below,
# so a drift in translation.py fails loudly instead of silently dropping a rule.
# --------------------------------------------------------------------------
REPHRASE = {
    "Do not translate English address markers such as sister, brother, bhai, ben, madam, or sir into caller labels like બહેન, ભાઈ, મેડમ, or સાહેબ. Use respectful gender-neutral 'આપ' wording instead.":
        "Do not address the caller with labels like બહેન, ભાઈ, મેડમ, or સાહેબ, even if the caller uses sister, brother, bhai, ben, madam, or sir. Use respectful gender-neutral 'આપ' wording instead.",
    "If the English source mentions 'sister' because the caller addressed Sarlaben, do not call the caller બહેન. Omit the address marker or render it as a neutral reference to સરલાબેન only when necessary.":
        "If the caller addresses Sarlaben as 'sister' or બહેન, do not call the caller બહેન. Omit the address marker or use a neutral reference to સરલાબેન only when necessary.",
    "'Amul AI', 'Amul A I', 'AMUL AI', 'AI helpline', 'amul helpline', 'AI helpline advisor', and 'AI-powered helpline' refer to the Amul Artificial Intelligence digital advisory helpline, not artificial insemination. Render as 'અમૂલ એ.આઈ.' / 'એ.આઈ. હેલ્પલાઇન'; never as 'કૃત્રિમ બીજદાન' or other insemination wording in helpline or assistant identity context.":
        "'Amul AI', 'Amul A I', 'AMUL AI', 'AI helpline', 'amul helpline', 'AI helpline advisor', and 'AI-powered helpline' refer to the Amul Artificial Intelligence digital advisory helpline, not artificial insemination. Say 'અમૂલ એ.આઈ.' / 'એ.આઈ. હેલ્પલાઇન'; never 'કૃત્રિમ બીજદાન' or other insemination wording in helpline or assistant identity context.",
}

RULES_HEADER = (
    "\n\n## GUJARATI TERMINOLOGY AND PERSONA RULES (MANDATORY)\n"
    "Every rule below applies to the Gujarati text you produce. Gujarati terms "
    "quoted here are exact: reproduce them character-for-character and never "
    "substitute a synonym.\n"
)


def build_rules_block() -> str:
    from app.services.translation import GU_PREFERRED_TRANSLATION_RULES as RULES
    missing = [k for k in REPHRASE if k not in RULES]
    if missing:
        raise SystemExit(
            "[fatal] REPHRASE key(s) not found verbatim in "
            f"GU_PREFERRED_TRANSLATION_RULES -- translation.py drifted:\n  "
            + "\n  ".join(r[:80] for r in missing)
        )
    lines = [REPHRASE.get(r, r) for r in RULES]
    print(f"[rules] imported {len(RULES)} rules, rephrased {len(REPHRASE)}")
    return RULES_HEADER + "\n".join(f"- {ln}" for ln in lines) + "\n"


# --------------------------------------------------------------------------
# Gate 2: deterministic forbidden-pair post-replacement.
# --------------------------------------------------------------------------
def build_map(policy_path: Path):
    with policy_path.open(encoding="utf-8") as f:
        pol = json.load(f)
    forb = pol.get("forbidden", {}) or {}
    pairs = sorted(
        [(str(k).strip(), str(v).strip()) for k, v in forb.items()
         if str(k).strip() and str(v).strip()],
        key=lambda kv: len(kv[0]), reverse=True,   # phrase-level beats word-level
    )
    print(f"[map] {len(pairs)} forbidden pairs, longest-key-first "
          f"(longest={len(pairs[0][0])} chars: {pairs[0][0]!r})")
    return pairs


def apply_map(text: str, pairs) -> str:
    for bad, good in pairs:
        if bad in text:
            text = text.replace(bad, good)
    return text


def _import_agent():
    from agents.voice import voice_agent
    from agents.deps import FarmerContext
    return voice_agent, FarmerContext


async def run_one(agent, FarmerContext, row_num, qid, question, lang, settings,
                  arm, sem, pairs):
    async with sem:
        t0 = time.perf_counter()
        row = {
            "row_num": row_num, "qid": qid, "arm": arm,
            "guided": bool(settings), "question": question, "answer": "",
            "latency_s": 0.0, "error": "", "num_tool_calls": 0,
            "tool_names": "", "tool_log": "", "answer_raw": "",
        }
        try:
            deps = FarmerContext(
                query=question, lang_code=lang, target_lang=lang,
                session_id=f"bench-{arm}-{row_num}",
            )
            kwargs = {"user_prompt": deps.get_user_message(), "deps": deps}
            if settings is not None:
                kwargs["model_settings"] = settings
            result = await agent.run(**kwargs)
            raw = (result.output if hasattr(result, "output") else str(result)) or ""
            row["answer_raw"] = raw
            row["answer"] = apply_map(raw, pairs) if pairs else raw

            names, log, n = [], [], 0
            try:
                for m in (result.all_messages() if hasattr(result, "all_messages") else []):
                    for p in getattr(m, "parts", []) or []:
                        ks = str(getattr(p, "part_kind", "") or type(p).__name__).lower()
                        if "tool-call" in ks:
                            n += 1
                            tn = getattr(p, "tool_name", "?")
                            names.append(tn)
                            log.append({"t": "call", "tool": tn,
                                        "args": str(getattr(p, "args", ""))[:600]})
                        elif "tool-return" in ks:
                            log.append({"t": "ret", "tool": getattr(p, "tool_name", "?"),
                                        "content": str(getattr(p, "content", ""))[:2000]})
            except Exception:
                pass
            row["num_tool_calls"] = n
            row["tool_names"] = "|".join(names)
            row["tool_log"] = json.dumps(log, ensure_ascii=False)
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {e}"
        row["latency_s"] = round(time.perf_counter() - t0, 3)
        return row


def load_questions(path, limit):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for i, r in enumerate(csv.DictReader(f)):
            q = next((r[c] for c in ("question", "question_gu", "query", "input")
                      if r.get(c)), "")
            if not q:
                continue
            rows.append((i, r.get("id", str(i)), q, (r.get("lang") or "").strip().lower()))
            if limit and len(rows) >= limit:
                break
    return rows


async def prove_injection(agent, FarmerContext, block, lang):
    """One real request; dump the instructions actually attached to the
    ModelRequest that went to gemma4. A silent no-op must not survive this."""
    probe = "તમે કોણ છો?"
    deps = FarmerContext(query=probe, lang_code=lang, target_lang=lang,
                         session_id="bench-proof")
    result = await agent.run(user_prompt=deps.get_user_message(), deps=deps)
    sent = ""
    for m in result.all_messages():
        sent = getattr(m, "instructions", None) or sent
        if sent:
            break
    if not sent:
        raise SystemExit("[fatal] could not read instructions off the ModelRequest")
    tail = sent[-1400:]
    print("=" * 72)
    print("[proof] instructions attached to the outgoing ModelRequest "
          f"({len(sent)} chars total). Last 1400 chars:")
    print(tail)
    print("=" * 72)
    needles = [
        "GUJARATI TERMINOLOGY AND PERSONA RULES",
        "શકતી છું",
        "NEVER use 'સ્તન' for animal udder",
        "Use 'બુલ' for bull",
        "Use 'ફેટ' for fat/milk-fat",
    ]
    for nd in needles:
        ok = nd in sent
        print(f"[proof] {'FOUND  ' if ok else 'MISSING'} {nd!r}")
        if not ok:
            raise SystemExit("[fatal] rules did NOT reach the model -- aborting")
    if block.strip() not in sent:
        raise SystemExit("[fatal] injected block not present verbatim -- aborting")
    print("[proof] full 33-rule block present verbatim in the sent instructions.")
    print(f"[proof] probe answer: {result.output[:300]}")


async def main_async(a):
    agent, FarmerContext = _import_agent()
    print(f"[ok] agent={getattr(agent, 'name', '?')}")

    block = ""
    if a.inject_rules:
        block = build_rules_block()
        # Runtime-only injection: register an extra instructions source on the
        # already-built Agent. The on-disk prompt file is never modified.
        def _injected_terminology_rules() -> str:
            return block
        agent.instructions(_injected_terminology_rules)
        print(f"[rules] injected {len(block)} chars at runtime "
              f"(on-disk prompt untouched)")

    pairs = []
    if a.apply_map:
        pairs = build_map(Path(a.policy))

    if a.dry_run:
        s = guided_settings(a.lang, extended=not a.strict_142)
        print(f"[dry-run] guided={'ON' if (a.guided and s) else 'OFF'}")
        if a.guided and s:
            print(f"[dry-run] pattern={s.get('extra_body')['structured_outputs']['regex'][:120]}")
        if a.inject_rules:
            await prove_injection(agent, FarmerContext, block, a.lang)
        return 0

    if a.inject_rules:
        await prove_injection(agent, FarmerContext, block, a.lang)

    rows = load_questions(a.questions, a.limit)

    def _settings_for(lang):
        if not a.guided:
            return None
        return guided_settings(lang or a.lang, extended=not a.strict_142)

    n_con = sum(1 for _, _, _, lg in rows if _settings_for(lg) is not None)
    print(f"[info] {len(rows)} rows | arm={a.arm} | guided_flag={a.guided} "
          f"| rows_constrained={n_con} | strict142={a.strict_142} "
          f"| concurrency={a.concurrency} | inject_rules={a.inject_rules} "
          f"| apply_map={a.apply_map}", flush=True)

    sem = asyncio.Semaphore(a.concurrency)
    tasks = [run_one(agent, FarmerContext, rn, qid, q, (lg or a.lang),
                     _settings_for(lg), a.arm, sem, pairs)
             for rn, qid, q, lg in rows]

    out, done = [], 0
    for coro in asyncio.as_completed(tasks):
        out.append(await coro)
        done += 1
        if done % 25 == 0 or done == len(tasks):
            errs = sum(1 for x in out if x["error"])
            print(f"[progress] {done}/{len(tasks)} errors={errs}", flush=True)

    out.sort(key=lambda r: r["row_num"])
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)
    errs = sum(1 for x in out if x["error"])
    changed = sum(1 for x in out if x["answer"] != x["answer_raw"])
    print(f"[map] rows changed by forbidden map: {changed}/{len(out)}")
    print(f"[done] {len(out)} rows -> {a.out} ({errs} errors)")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--questions", required=False, default="")
    p.add_argument("--out", default="bench_out_voice_parity.csv")
    p.add_argument("--arm", default="voice_parity")
    p.add_argument("--lang", default="gu")
    p.add_argument("--guided", action="store_true")
    p.add_argument("--strict-142", action="store_true",
                   help="use #142's char set verbatim (omits the degree sign)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--inject-rules", action="store_true",
                   help="append the 33 GU_PREFERRED_TRANSLATION_RULES to the "
                        "agent's instructions at runtime")
    p.add_argument("--apply-map", action="store_true",
                   help="apply gu_term_policy.json forbidden pairs post-hoc")
    p.add_argument("--policy", default="assets/gu_term_policy.json")
    a = p.parse_args()
    if not a.dry_run and not a.questions:
        p.error("--questions required unless --dry-run")
    sys.exit(asyncio.run(main_async(a)))


if __name__ == "__main__":
    main()
