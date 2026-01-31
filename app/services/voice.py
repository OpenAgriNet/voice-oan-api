from typing import AsyncGenerator, Optional, Literal
# from fastapi import BackgroundTasks
from agents.voice import voice_agent
from agents.tools.farmer import normalize_phone_to_mobile, fetch_farmer_info_raw
from helpers.utils import get_logger
from app.utils import (
    update_message_history, 
    trim_history, 
    format_message_pairs,
    clean_message_history_for_openai
)
# NOTE: Removing telemetry for now.
# from app.tasks.telemetry import send_telemetry
from agents.deps import FarmerContext

logger = get_logger(__name__)

async def stream_voice_message(
    query: str,
    session_id: str,
    source_lang: str,
    target_lang: str,
    user_id: str,
    history: list,
    provider: Optional[Literal['RAYA']] = None,
    process_id: Optional[str] = None,
    user_info: dict = None,
#    background_tasks: BackgroundTasks,
    
) -> AsyncGenerator[str, None]:
    """Async generator for streaming chat messages."""
    # user_id is expected to be phone number: clean it and proactively fetch farmer info
    mobile = normalize_phone_to_mobile(user_id)
    farmer_info = None
    if mobile:
        records = await fetch_farmer_info_raw(mobile)
        if records:
            farmer_info = {"farmers": records}  # wrap list for FarmerContext (expects Dict)
            logger.info(f"Proactively loaded farmer info for mobile {mobile}")
        else:
            # API failed: set default message in target language so agent continues
            msg = (
                "Could not fetch farmer data, continuing to call"
                if (target_lang or "gu") != "gu"
                else "કૃષિકાર ડેટા મેળવી શકાયો નથી, કૉલ ચાલુ રાખી રહ્યા છીએ"
            )
            farmer_info = {"message": msg}

    # Generate a unique content ID for this query
    content_id = f"query_{session_id}_{len(history)//2 + 1}"
    logger.info(f"User info: {user_info}")
    deps = FarmerContext(query=query,
                         lang_code=source_lang,
                         target_lang=target_lang,
                         provider=provider,
                         session_id=session_id,
                         process_id=process_id,
                         farmer_info=farmer_info
                         )

    message_pairs = "\n\n".join(format_message_pairs(history, 3))
    logger.info(f"Message pairs: {message_pairs}")
    if message_pairs:
        last_response = f"**Conversation**\n\n{message_pairs}\n\n---\n\n"
    else:
        last_response = ""
    
    user_message = deps.get_user_message()
    logger.info(f"Running agent with user message: {user_message}")

    # Clean message history to remove orphaned tool calls BEFORE passing to OpenAI API
    cleaned_history = clean_message_history_for_openai(history)
    if len(cleaned_history) != len(history):
        logger.warning(f"Cleaned {len(history) - len(cleaned_history)} orphaned tool calls from history")
        # Update the history in cache with cleaned version
        await update_message_history(session_id, cleaned_history)
        history = cleaned_history
    
    # Run the main agent
    trimmed_history = trim_history(
        history,
        max_tokens=80_000,
        include_system_prompts=True,
        include_tool_calls=True
    )
    
    logger.info(f"Trimmed history length: {len(trimmed_history)} messages")

    async with voice_agent.run_stream(
        user_prompt=user_message,
        message_history=trimmed_history,
        deps=deps,
    ) as response_stream:
        async for chunk in response_stream.stream_text(delta=True):
            # if chunk:
            yield chunk
        
        logger.info(f"Streaming complete for session {session_id}")
        # Capture the data we need while response_stream is still available
        new_messages = response_stream.new_messages()

    # Post-processing happens AFTER streaming is complete
    messages = [
        *history,
        *new_messages
    ]

    logger.info(f"Updating message history for session {session_id} with {len(messages)} messages")
    await update_message_history(session_id, messages)