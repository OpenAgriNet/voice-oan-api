"""
Test the voice agent with streaming output.

Loads questions from tests/fixtures/sample_questions.json,
runs each through the voice_agent using run_stream_events(),
logs streaming output, and saves results.

Usage:
    python tests/test_voice_streaming.py
    python tests/test_voice_streaming.py --sample 5
    python tests/test_voice_streaming.py --category crop_advisory
"""
import asyncio
import argparse
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from agents.voice import voice_agent, VoiceOutput
from agents.deps import FarmerContext


def load_questions(filepath: str, sample: int = None, category: str = None, language: str = None) -> list:
    """Load questions from JSON file with optional filters and random sampling."""
    with open(filepath, "r", encoding="utf-8") as f:
        questions = json.load(f)

    if category:
        questions = [q for q in questions if q.get("category") == category]
    if language:
        questions = [q for q in questions if q.get("language") == language]
    if sample and sample < len(questions):
        questions = random.sample(questions, sample)

    return questions


def _extract_audio_from_partial_json(text: str) -> str:
    """Extract the audio field from partial/incomplete JSON text."""
    match = re.search(r'"audio"\s*:\s*"((?:[^"\\]|\\.)*)', text)
    return match.group(1) if match else ""


async def run_streaming_test(question: str, language: str, session_id: str) -> dict:
    """Run a single question through the voice agent with run_stream_events()."""
    deps = FarmerContext(query=question, lang_code=language, session_id=session_id)
    user_message = deps.get_user_message()

    result = {
        "question": question,
        "language": language,
        "status": "unknown",
        "final_output": None,
        "stream_count": 0,
        "duration_ms": 0,
        "tool_calls": 0,
        "error": None,
    }

    start = time.monotonic()
    text_buffer = ""
    prev_audio = ""

    try:
        async for event in voice_agent.run_stream_events(
            user_prompt=user_message,
            message_history=[],
            deps=deps
        ):
            kind = getattr(event, 'event_kind', '')

            if kind == 'part_delta':
                delta = event.delta
                if getattr(delta, 'part_delta_kind', '') == 'text':
                    text_buffer += delta.content_delta
                    audio = _extract_audio_from_partial_json(text_buffer)
                    if audio and audio != prev_audio:
                        result["stream_count"] += 1
                        prev_audio = audio
                        print(f"    [stream {result['stream_count']}] audio: {audio[:80]}...")

            elif kind == 'function_tool_call':
                tool_name = event.part.tool_name
                result["tool_calls"] += 1
                print(f"    [tool] {tool_name}")

            elif kind == 'function_tool_result':
                # Reset text buffer for next model turn
                text_buffer = ""
                prev_audio = ""

            elif kind == 'agent_run_result':
                output = event.result.output
                result["final_output"] = {
                    "audio": output.audio,
                    "end_interaction": output.end_interaction,
                }
                result["status"] = "success"

    except asyncio.TimeoutError:
        result["status"] = "timeout"
        result["error"] = "Request timed out"
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {str(e)}"
        import traceback
        traceback.print_exc()

    result["duration_ms"] = round((time.monotonic() - start) * 1000)
    return result


async def main():
    parser = argparse.ArgumentParser(description="Test voice agent with streaming")
    parser.add_argument("--sample", type=int, default=None, help="Randomly sample N questions")
    parser.add_argument("--category", type=str, default=None, help="Filter by category")
    parser.add_argument("--language", type=str, default=None, help="Filter by language (en/hi)")
    parser.add_argument("--timeout", type=float, default=120.0, help="Timeout per question (seconds)")
    args = parser.parse_args()

    # Load questions
    fixtures_path = os.path.join(os.path.dirname(__file__), "fixtures", "sample_questions.json")
    questions = load_questions(fixtures_path, sample=args.sample, category=args.category, language=args.language)

    print(f"Loaded {len(questions)} questions")
    print("=" * 70)

    results = []

    for i, q in enumerate(questions):
        session_id = f"test-{i}"
        print(f"\n[{i+1}/{len(questions)}] [{q['language'].upper()}] {q['question'][:60]}...")

        result = await asyncio.wait_for(
            run_streaming_test(q["question"], q["language"], session_id),
            timeout=args.timeout
        )
        result["category"] = q.get("category")
        result["subcategory"] = q.get("subcategory")
        results.append(result)

        # Print summary
        icon = "OK" if result["status"] == "success" else result["status"].upper()
        print(f"  -> [{icon}] {result['duration_ms']}ms | streams: {result['stream_count']} | tools: {result['tool_calls']}")

        if result["error"]:
            print(f"  -> Error: {result['error'][:100]}")
        elif result["final_output"]:
            audio_preview = result["final_output"]["audio"][:120] if result["final_output"]["audio"] else ""
            print(f"  -> Audio: {audio_preview}...")
            print(f"  -> end_interaction: {result['final_output']['end_interaction']}")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    total = len(results)
    success = sum(1 for r in results if r["status"] == "success")
    failed = total - success
    avg_ms = sum(r["duration_ms"] for r in results) / max(total, 1)
    avg_streams = sum(r["stream_count"] for r in results) / max(total, 1)
    avg_tools = sum(r["tool_calls"] for r in results) / max(total, 1)

    print(f"Total: {total} | Success: {success} | Failed: {failed}")
    print(f"Avg duration: {avg_ms:.0f}ms | Avg streams: {avg_streams:.1f} | Avg tools: {avg_tools:.1f}")

    if failed > 0:
        print("\nFailed questions:")
        for r in results:
            if r["status"] != "success":
                print(f"  - [{r['status']}] {r['question'][:50]} -> {r.get('error', '')[:80]}")

    # Save results
    output_path = os.path.join(os.path.dirname(__file__), "streaming_test_results.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
