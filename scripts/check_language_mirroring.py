"""Ad-hoc check: does the voice endpoint reply in the language it was asked in?

Sends one farming question per supported language with NO X-Language header and
asserts the reported `language` matches. Run against a locally started API:

    AUTH_ENABLED=false REDIS_HOST=localhost \
        python -m uvicorn main:app --host 127.0.0.1 --port 8020

    python scripts/check_language_mirroring.py [base_url]
"""
import asyncio
import json
import sys
import time

import httpx

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8020"
ENDPOINT = f"{BASE_URL}/api/v1/chat-dev/completions"

# One "when should I sow wheat?"-style question per language.
QUERIES: dict[str, str] = {
    "en": "When should I sow wheat in my field?",
    "hi": "मुझे अपने खेत में गेहूं कब बोना चाहिए?",
    "bn": "আমার জমিতে গম কখন বপন করা উচিত?",
    "te": "నా పొలంలో గోధుమలు ఎప్పుడు విత్తాలి?",
    "mr": "माझ्या शेतात गहू कधी पेरावा?",
    "ta": "என் வயலில் கோதுமையை எப்போது விதைக்க வேண்டும்?",
    "gu": "મારા ખેતરમાં ઘઉં ક્યારે વાવવા જોઈએ?",
    "kn": "ನನ್ನ ಹೊಲದಲ್ಲಿ ಗೋಧಿಯನ್ನು ಯಾವಾಗ ಬಿತ್ತಬೇಕು?",
    "ml": "എന്റെ വയലിൽ ഗോതമ്പ് എപ്പോൾ വിതയ്ക്കണം?",
    "pa": "ਮੈਨੂੰ ਆਪਣੇ ਖੇਤ ਵਿੱਚ ਕਣਕ ਕਦੋਂ ਬੀਜਣੀ ਚਾਹੀਦੀ ਹੈ?",
    "od": "ମୁଁ ମୋ ଜମିରେ ଗହମ କେବେ ବୁଣିବା ଉଚିତ?",
}


async def ask(client: httpx.AsyncClient, lang: str, query: str) -> dict:
    """Send one query with no language header and return the parsed voice payload."""
    started = time.monotonic()
    resp = await client.post(
        ENDPOINT,
        headers={
            "X-Tenant-ID": "lang-check",
            "X-User-ID": "lang-check",
            "X-Session-ID": f"langcheck-{lang}-{int(time.time())}",
        },
        json={"messages": [{"role": "user", "content": query}], "stream": False},
        timeout=180.0,
    )
    elapsed = time.monotonic() - started
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    payload = json.loads(content)
    return {
        "expected": lang,
        "reported": payload.get("language"),
        "audio": payload.get("audio", ""),
        "elapsed": elapsed,
    }


async def main() -> int:
    async with httpx.AsyncClient() as client:
        # Sequential: the upstream search backend throttles above ~5 concurrent calls.
        results = []
        for lang, query in QUERIES.items():
            try:
                results.append(await ask(client, lang, query))
            except Exception as exc:  # noqa: BLE001 - ad-hoc script, report and continue
                results.append({"expected": lang, "reported": f"ERROR: {exc}", "audio": "", "elapsed": 0.0})

    failures = 0
    for r in results:
        ok = r["expected"] == r["reported"]
        failures += 0 if ok else 1
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {r['expected']} -> {r['reported']}  ({r['elapsed']:.1f}s)")
        print(f"       {r['audio'][:160]}")

    print(f"\n{len(results) - failures}/{len(results)} languages mirrored correctly")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
