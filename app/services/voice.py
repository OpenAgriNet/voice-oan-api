from typing import AsyncGenerator, Optional, Literal
# from fastapi import BackgroundTasks
from agents.voice import voice_agent
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
from app.services.translation import translation_service

logger = get_logger(__name__)

# Default trim history configuration for voice endpoints
def _trim_voice_history(history: list) -> list:
    """
    Helper function to trim history with standard voice endpoint settings.
    
    Args:
        history: Message history to trim
        
    Returns:
        Trimmed message history
    """
    return trim_history(
        history,
        max_tokens=80_000,
        include_system_prompts=True,
        include_tool_calls=True
    )

async def stream_voice_message(
    query: str,
    session_id: str,
    source_lang: str,
    target_lang: str,
#    user_id: str,
    history: list,
    provider: Optional[Literal['RAYA', 'RINGG']] = None,
    process_id: Optional[str] = None,
#    user_info: dict,
#    background_tasks: BackgroundTasks,
    
) -> AsyncGenerator[str, None]:
    """Async generator for streaming chat messages."""
    # Generate a unique content ID for this query
    content_id = f"query_{session_id}_{len(history)//2 + 1}"
#    logger.info(f"User info: {user_info}")
    deps = FarmerContext(query=query,
                         lang_code=source_lang,
                         target_lang=target_lang,
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
    trimmed_history = _trim_voice_history(history)
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


async def get_voice_message_with_translation(
    query: str,
    session_id: str,
    source_lang: str,
    target_lang: str,
    history: list,
    provider: Optional[Literal['RAYA', 'RINGG']] = None,
    process_id: Optional[str] = None,
) -> str:
    logger.info(f"Translating query from {source_lang} to en (English)")
    translated_query = await translation_service.translate_text(
        text=query,
        source_lang=source_lang,
        target_lang='en'
    )
    logger.info(f"Translated query: {translated_query}")
    
    # Use English as the source_lang for the agent since we translated the query
    deps = FarmerContext(
        query=translated_query,
        lang_code='en',
        target_lang='en',  
        provider=provider,
        session_id=session_id,
        process_id=process_id
    )

    message_pairs = "\n\n".join(format_message_pairs(history, 3))
    logger.info(f"Message pairs: {message_pairs}")
    
    user_message = deps.get_user_message()
    logger.info(f"Running agent with translated user message: {user_message}")

    # Clean message history to remove orphaned tool calls BEFORE passing to OpenAI API
    cleaned_history = clean_message_history_for_openai(history)
    if len(cleaned_history) != len(history):
        logger.warning(f"Cleaned {len(history) - len(cleaned_history)} orphaned tool calls from history")
        # Update the history in cache with cleaned version
        await update_message_history(session_id, cleaned_history)
        history = cleaned_history
    
    # Run the main agent
    trimmed_history = _trim_voice_history(history)
    logger.info(f"Trimmed history length: {len(trimmed_history)} messages")

    # Collect the full response from the agent
    full_response = ""
    new_messages = None
    
    async with voice_agent.run_stream(
        user_prompt=user_message,
        message_history=trimmed_history,
        deps=deps,
    ) as response_stream:
        async for chunk in response_stream.stream_text(delta=True):
            if chunk:
                full_response += chunk
        
        logger.info(f"Response collection complete for session {session_id}")
        # Capture the data we need while response_stream is still available
        new_messages = response_stream.new_messages()

    # Translate the response back to source_lang
    if full_response:
        # Always translate back to source_lang, even if source_lang is 'mr'
        # (translation service will handle no-op case)
        logger.info(f"Translating response from en (English) to {source_lang}")
        translated_response = await translation_service.translate_text(
            text=full_response,
            source_lang='en',
            target_lang=source_lang
        )
        logger.info(f"Successfully translated response to {source_lang}. Length: {len(translated_response)} chars")
        logger.debug(f"Translated response preview: {translated_response[:200]}...")
        
        # Post-processing: Update message history with original query and translated response
        # Note: We store the original query and the translated response in history
        # The agent's internal messages are in English, but we expose the translated version
        messages = [
            *history,
            *new_messages
        ]

        logger.info(f"Updating message history for session {session_id} with {len(messages)} messages")
        await update_message_history(session_id, messages)
        
        return translated_response
    else:
        logger.warning("Empty response from agent, nothing to translate")
        
        # Update message history even if response is empty
        messages = [
            *history,
            *new_messages
        ]
        await update_message_history(session_id, messages)
        
        return ""