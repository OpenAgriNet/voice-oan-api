import asyncio
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.voice import stream_voice_message
from app.utils import _get_message_history, claim_session_request_ownership


FIRST_TURN_QUERY = "hey,how are you?"
GU_FIRST_TURN_QUERY = "હે સરલાબેન, કેમ છો?"

CLOSING_QUERIES = [
    "ok",
    #"ok thanks, bye",
    #"ok. I dont need anything else",
    #"bye",
    "thanks",
    #"thank you, bye",
    #"no, that is all",
    #"no thanks",
    #"i am done",
    #"thats all for now",
    #"all right, bye",
    #"ok got it, thanks",
    #"nothing else",
    # "no more questions",
    "perfect",
    "perfect, bye",
]

GU_CLOSING_QUERIES = [
    "ઠીક છે",
    "અચ્છા",
    "બરાબર, આભાર",
    "હા ઠીક છે, બસ",
    "ઓકે, હવે કઈ જરૂર નથી",
]


async def _run_turn(
    *,
    session_id: str,
    query: str,
    process_id: str,
    user_id: str,
    source_lang: str = "en",
    target_lang: str = "en",
) -> str:
    owner = await claim_session_request_ownership(session_id)
    history = await _get_message_history(session_id)
    chunks: list[str] = []
    async for chunk in stream_voice_message(
        query=query,
        session_id=session_id,
        source_lang=source_lang,
        target_lang=target_lang,
        user_id=user_id,
        history=history,
        provider="RAYA",
        process_id=process_id,
        user_info={"user_id": user_id},
        owner=owner,
        http_request=None,
    ):
        if isinstance(chunk, str):
            chunks.append(chunk)
    return "".join(chunks).strip()


async def run_matrix() -> None:
    user_id = "9924457046"
    run_id = int(time.time())
    results: list[tuple[int, str, bool, str]] = []

    for idx, closing_query in enumerate(CLOSING_QUERIES, start=1):
        session_id = f"test-goodbye-matrix-{run_id}-{idx}"
        print(f"\n--- Case {idx}/15 ---", flush=True)
        print(f"session_id: {session_id}", flush=True)
        print(f"turn-1: {FIRST_TURN_QUERY}", flush=True)

        first_response = await _run_turn(
            session_id=session_id,
            query=FIRST_TURN_QUERY,
            process_id=f"{idx}-1",
            user_id=user_id,
        )
        print(f"turn-1-response: {first_response}", flush=True)

        print(f"turn-2: {closing_query}", flush=True)
        second_response = await _run_turn(
            session_id=session_id,
            query=closing_query,
            process_id=f"{idx}-2",
            user_id=user_id,
        )
        has_goodbye = "Goodbye." in second_response or second_response.rstrip().endswith("Goodbye")
        results.append((idx, closing_query, has_goodbye, second_response))
        print(f"turn-2-response: {second_response}", flush=True)
        print(f"goodbye_streamed: {has_goodbye}", flush=True)

    print("\n=== Goodbye Stream Summary ===", flush=True)
    for idx, closing_query, has_goodbye, _ in results:
        print(f"{idx:02d}. {closing_query!r} -> {has_goodbye}", flush=True)
    total = sum(1 for _, _, ok, _ in results if ok)
    print(f"\nTotal cases with Goodbye streamed: {total}/{len(results)}", flush=True)


async def run_gujarati_matrix() -> None:
    user_id = "9924457046"
    run_id = int(time.time())
    results: list[tuple[int, str, bool, str]] = []

    for idx, closing_query in enumerate(GU_CLOSING_QUERIES, start=1):
        session_id = f"test-goodbye-gu-{run_id}-{idx}"
        print(f"\n--- Gujarati Case {idx}/{len(GU_CLOSING_QUERIES)} ---", flush=True)
        print(f"session_id: {session_id}", flush=True)
        print(f"turn-1: {GU_FIRST_TURN_QUERY}", flush=True)

        first_response = await _run_turn(
            session_id=session_id,
            query=GU_FIRST_TURN_QUERY,
            process_id=f"gu-{idx}-1",
            user_id=user_id,
            source_lang="gu",
            target_lang="gu",
        )
        print(f"turn-1-response: {first_response}", flush=True)

        print(f"turn-2: {closing_query}", flush=True)
        second_response = await _run_turn(
            session_id=session_id,
            query=closing_query,
            process_id=f"gu-{idx}-2",
            user_id=user_id,
            source_lang="gu",
            target_lang="gu",
        )
        has_goodbye = "Goodbye." in second_response or second_response.rstrip().endswith("Goodbye")
        results.append((idx, closing_query, has_goodbye, second_response))
        print(f"turn-2-response: {second_response}", flush=True)
        print(f"goodbye_streamed: {has_goodbye}", flush=True)

    print("\n=== Gujarati Goodbye Stream Summary ===", flush=True)
    for idx, closing_query, has_goodbye, _ in results:
        print(f"{idx:02d}. {closing_query!r} -> {has_goodbye}", flush=True)
    total = sum(1 for _, _, ok, _ in results if ok)
    print(f"\nTotal Gujarati cases with Goodbye streamed: {total}/{len(results)}", flush=True)


async def run_all_matrices() -> None:
    """Run English then Gujarati suites in one event loop (required for Redis)."""
    await run_matrix()
    await run_gujarati_matrix()


if __name__ == "__main__":
    asyncio.run(run_all_matrices())