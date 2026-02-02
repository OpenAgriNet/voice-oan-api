#!/usr/bin/env python3
"""
Validate feedback gathering mechanism against dev API.
Per FEEDBACK_IMPLENETATION_NOTES.md, feedback should trigger on:
A. Task completion / conversation_closing (user says No, Thanks, Goodbye after task)
B. User frustration (user corrects agent, repeats request)
C. Session end

Uses provider=RAYA (required for feedback prompt to be sent).
"""
import os
import sys
import uuid
import urllib.parse
import time

# Add project root for imports if needed
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_URL = os.getenv("VOICE_API_URL", "https://dev-amulmitra.amul.com/api/voice")
# RAYA is REQUIRED for feedback to trigger (see voice.py: provider == "RAYA")
PROVIDER = "RAYA"


def call_voice(query: str, session_id: str | None = None, source_lang: str = "en", target_lang: str = "en") -> tuple[str, str]:
    """Call voice API, return (session_id_used, full_response_text)."""
    import httpx

    sid = session_id or str(uuid.uuid4())
    params = {
        "query": query,
        "session_id": sid,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "user_id": "test-feedback-validation",
        "provider": PROVIDER,
    }
    url = f"{BASE_URL}/?{urllib.parse.urlencode(params)}"
    with httpx.Client(timeout=60.0) as client:
        r = client.get(url)
        r.raise_for_status()
        # Response is SSE stream; collect all data
        text = r.text
    return sid, text


def extract_sse_text(sse_body: str) -> str:
    """Extract concatenated text from SSE stream (simple parsing)."""
    lines = sse_body.strip().split("\n")
    parts = []
    for line in lines:
        if line.startswith("data:"):
            d = line[5:].strip()
            if d and d != "[DONE]":
                parts.append(d)
    return " ".join(parts) if parts else sse_body[:500]


def main():
    results = []
    print(f"=== Feedback Validation ===\nBase URL: {BASE_URL}\nProvider: {PROVIDER} (required)\n")

    # Scenario 1: Task completion -> User says "No" to "Do you need any other information?"
    print("\n--- Scenario 1: Task completion + User declines (No) ---")
    s1 = str(uuid.uuid4())
    _, r1 = call_voice("How to treat mastitis in cows?", session_id=s1)
    t1 = extract_sse_text(r1)
    print(f"Turn 1 response (preview): {t1[:200]}...")
    time.sleep(1)
    _, r2 = call_voice("No", session_id=s1)
    t2 = extract_sse_text(r2)
    print(f"Turn 2 ('No') response (preview): {t2[:300]}...")
    time.sleep(1)
    _, r3 = call_voice("4", session_id=s1)
    t3 = extract_sse_text(r3)
    feedback_triggered = "feedback" in t3.lower() or "thank" in t3.lower() or "આભાર" in t3 or "improve" in t3.lower()
    print(f"Turn 3 ('4' rating) response: {t3[:200]}")
    results.append({
        "scenario": "1_task_completion_user_says_no",
        "session_id": s1,
        "feedback_triggered": feedback_triggered,
        "turn3_preview": t3[:200],
    })
    print(f"Session ID: {s1}")
    print(f"Feedback triggered (ack received): {feedback_triggered}")

    # Scenario 2: Explicit call end - "Thanks" / "Goodbye"
    print("\n--- Scenario 2: Explicit call end (Thanks) ---")
    s2 = str(uuid.uuid4())
    call_voice("What is lumpy skin disease?", session_id=s2)
    time.sleep(1)
    call_voice("Thanks, that's all", session_id=s2)
    time.sleep(1)
    _, r2b = call_voice("5", session_id=s2)
    t2b = extract_sse_text(r2b)
    feedback_triggered_2 = "thank" in t2b.lower() or "આભાર" in t2b or "improve" in t2b.lower()
    results.append({
        "scenario": "2_explicit_call_end_thanks",
        "session_id": s2,
        "feedback_triggered": feedback_triggered_2,
        "turn3_preview": t2b[:200],
    })
    print(f"Session ID: {s2}")
    print(f"Feedback triggered: {feedback_triggered_2}")

    # Scenario 3: User frustration - "That's not what I meant"
    print("\n--- Scenario 3: User frustration ---")
    s3 = str(uuid.uuid4())
    call_voice("How to increase milk in buffalo?", session_id=s3)
    time.sleep(1)
    _, r3a = call_voice("No that's not what I meant", session_id=s3)
    t3a = extract_sse_text(r3a)
    time.sleep(1)
    _, r3b = call_voice("3", session_id=s3)
    t3b = extract_sse_text(r3b)
    feedback_triggered_3 = "sorry" in t3b.lower() or "દુઃખ" in t3b or "improve" in t3b.lower()
    results.append({
        "scenario": "3_user_frustration",
        "session_id": s3,
        "feedback_triggered": feedback_triggered_3,
        "turn3_preview": t3b[:200],
    })
    print(f"Session ID: {s3}")
    print(f"Turn 2 response (preview): {t3a[:150]}...")
    print(f"Feedback triggered: {feedback_triggered_3}")

    # Scenario 4: Gujarati - "ના" (No)
    print("\n--- Scenario 4: Gujarati - User says ના (No) ---")
    s4 = str(uuid.uuid4())
    call_voice("ભેંસમાં દૂધ કેવી રીતે વધારવું?", session_id=s4, source_lang="gu", target_lang="gu")
    time.sleep(1)
    call_voice("ના", session_id=s4, source_lang="gu", target_lang="gu")
    time.sleep(1)
    _, r4 = call_voice("પાંચ", session_id=s4, source_lang="gu", target_lang="gu")
    t4 = extract_sse_text(r4)
    feedback_triggered_4 = "આભાર" in t4 or "feedback" in t4.lower() or "આનંદ" in t4
    results.append({
        "scenario": "4_gujarati_user_says_no",
        "session_id": s4,
        "feedback_triggered": feedback_triggered_4,
        "turn3_preview": t4[:200],
    })
    print(f"Session ID: {s4}")
    print(f"Feedback triggered: {feedback_triggered_4}")

    # Scenario 5: Provider NOT RAYA - feedback should NOT trigger
    print("\n--- Scenario 5: provider=omit (feedback should NOT trigger) ---")
    s5 = str(uuid.uuid4())
    import httpx
    params = {
        "query": "How to treat mastitis?",
        "session_id": s5,
        "source_lang": "en",
        "target_lang": "en",
        "user_id": "test",
    }
    url = f"{BASE_URL}/?{urllib.parse.urlencode(params)}"
    with httpx.Client(timeout=60.0) as client:
        r = client.get(url)
        r.raise_for_status()
        text5a = r.text
    time.sleep(1)
    params["query"] = "No"
    url = f"{BASE_URL}/?{urllib.parse.urlencode(params)}"
    with httpx.Client(timeout=60.0) as client:
        r = client.get(url)
        text5b = r.text
    time.sleep(1)
    params["query"] = "4"
    url = f"{BASE_URL}/?{urllib.parse.urlencode(params)}"
    with httpx.Client(timeout=60.0) as client:
        r = client.get(url)
        text5c = r.text
    t5c = extract_sse_text(text5c)
    # Without RAYA, next turn after "No" should NOT be feedback capture - we'd get agent response to "4"
    no_feedback_expected = "4" in t5c or "milk" in t5c.lower() or "help" in t5c.lower()  # agent might try to answer "4"
    results.append({
        "scenario": "5_no_provider_no_feedback",
        "session_id": s5,
        "feedback_should_not_trigger": True,
        "got_ack_instead_of_agent": "thank" in t5c.lower() or "આભાર" in t5c,  # if we got ack, feedback triggered (unexpected)
    })
    print(f"Session ID: {s5}")
    print(f"Without provider=RAYA, got ack (feedback triggered): {results[-1]['got_ack_instead_of_agent']}")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for r in results:
        sid = r.get("session_id", "N/A")
        scen = r.get("scenario", "?")
        if "feedback_triggered" in r:
            status = "OK" if r["feedback_triggered"] else "FAIL"
            print(f"{scen}: {status} (session_id={sid})")
        else:
            ok = not r.get("got_ack_instead_of_agent", False)
            status = "OK (no feedback as expected)" if ok else "FAIL (feedback triggered)"
            print(f"{scen}: {status} (session_id={sid})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
