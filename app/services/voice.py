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
    history: list,
    provider: Optional[Literal['RAYA', 'RINGG']] = None,
    process_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Async generator for streaming chat messages."""
    # Generate a unique content ID for this query
    content_id = f"query_{session_id}_{len(history)//2 + 1}"
    logger.info(f"User info: {user_info}")
    deps = FarmerContext(query=query,
                         lang_code=source_lang,
                         target_lang=target_lang,
                         provider=provider,
                         session_id=session_id,
                         process_id=process_id
                         )

    message_pairs = "\n\n".join(format_message_pairs(history, 3))
    logger.info(f"Message pairs: {message_pairs}")
    
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
    history: list,
    provider: Optional[Literal['RAYA', 'RINGG']] = None,
    process_id: Optional[str] = None,
) -> str:
    logger.info(f"Translating query from `bhb` to `en` (Bhashini)")
    translated_query = await translation_service.translate_text(
        text=query,
        source_lang='bhb',
        target_lang='en'
    )
    logger.info(f"Translated query: {translated_query}")
    
    # Use English as the source_lang for the agent since we translated the query
    deps = FarmerContext(
        query=translated_query,
        lang_code='en',
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

    response = await voice_agent.run(
        user_prompt=user_message,
        message_history=trimmed_history,
        deps=deps,
    )
    text_response = response.output
    logger.info(f"Text response: {text_response}")

    # Translate the response back to source_lang
    if response.output:
        # Always translate back to source_lang, even if source_lang is 'mr'
        # (translation service will handle no-op case)
        logger.info(f"Translating response from `en` (English) to `bhb` (Bhashini)")
        translated_response = await translation_service.translate_text(
            text=text_response,
            source_lang='en',
            target_lang='bhb'
        )
        logger.info(f"Successfully translated response to `bhb`. Length: {len(translated_response)} chars")
        logger.debug(f"Translated response preview: {translated_response[:200]}...")
        
        return translated_response
    else:
        logger.warning("Empty response from agent, nothing to translate")
        return ""