from typing import AsyncGenerator, Optional, Literal
# from fastapi import BackgroundTasks
from agents.voice import voice_agent
# NOTE: Removed moderation for now. Directly running the voice agent for lower latencies.
# from agents.moderation import moderation_agent
from helpers.utils import get_logger
from app.utils import (
    update_message_history, 
    trim_history, 
    format_message_pairs,
    clean_message_history_for_openai
)
# NOTE: Removing telemetry for now.
# from helpers.telemetry import create_moderation_event
# from app.tasks.telemetry import send_telemetry
# NOTE: Removing suggestions for now.
# from app.tasks.suggestions import create_suggestions
from agents.deps import FarmerContext

logger = get_logger(__name__)

async def stream_voice_message(
    query: str,
    session_id: str,
    source_lang: str,
    target_lang: str,
    user_id: str,
    history: list,
    provider: Optional[Literal['RAYA', 'RINGG']] = None,
    process_id: Optional[str] = None,
    user_info: dict = None,
#    background_tasks: BackgroundTasks,
    
) -> AsyncGenerator[str, None]:
    """Async generator for streaming chat messages."""
    # Generate a unique content ID for this query
    content_id = f"query_{session_id}_{len(history)//2 + 1}"
    logger.info(f"User info: {user_info}")
    deps = FarmerContext(query=query,
                         lang_code=target_lang,
                         provider=provider,
                         session_id=session_id,
                         process_id=process_id
                         )

    message_pairs = "\n\n".join(format_message_pairs(history, 3))
    logger.info(f"Message pairs: {message_pairs}")
    if message_pairs:
        last_response = f"**Conversation**\n\n{message_pairs}\n\n---\n\n"
    else:
        last_response = ""
    
    user_message    = f"{last_response}{deps.get_user_message()}"
    
    # NOTE: Removed moderation for now. Directly running the voice agent for lower latencies.
    # moderation_run  = await moderation_agent.run(user_message)
    # moderation_data = moderation_run.output
    # logger.info(f"Moderation data: {moderation_data}")
    # deps.update_moderation_str(str(moderation_data))

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