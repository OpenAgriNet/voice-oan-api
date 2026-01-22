from typing import AsyncGenerator
from agents.voice import voice_agent, VoiceOutput
# from agents.moderation import moderation_agent  # Moderation disabled
from helpers.utils import get_logger
from app.utils import (
    update_message_history, 
    trim_history, 
    format_message_pairs
)
# from app.tasks.suggestions import create_suggestions  # Commented out: suggestion agent disabled
from agents.deps import FarmerContext
from pydantic_ai import (
    Agent,
    FinalResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPartDelta,
    ThinkingPartDelta,
)
from pydantic_ai.messages import TextPart, ThinkingPart
import json  # Added for JSON serialization

logger = get_logger(__name__)

async def stream_voice_message(
    query: str,
    session_id: str,
    source_lang: str,
    target_lang: str,
    user_id: str,
    history: list
) -> AsyncGenerator[str, None]:
    """Async generator for streaming voice messages."""
    # Generate a unique content ID for this query
    content_id = f"query_{session_id}_{len(history)//2 + 1}"
       
    deps = FarmerContext(query=query, lang_code=target_lang, session_id=session_id)

    message_pairs = "\n\n".join(format_message_pairs(history, 3))
    logger.info(f"Message pairs: {message_pairs}")
    if message_pairs:
        last_response = f"**Conversation**\n\n{message_pairs}\n\n---\n\n"
    else:
        last_response = ""
    
    user_message    = f"{last_response}{deps.get_user_message()}"

    user_message = deps.get_user_message()
    logger.info(f"Running agent with user message: {user_message}")

    # Run the main agent
    trimmed_history = trim_history(
        history,
        max_tokens=80_000,
        include_system_prompts=True,
        include_tool_calls=True
    )
    
    logger.info(f"Trimmed history length: {len(trimmed_history)} messages")

    async with voice_agent.iter(user_prompt=user_message, message_history=trimmed_history, deps=deps) as agent_run:
        async for node in agent_run:
            if Agent.is_user_prompt_node(node):
                logger.info(f"User prompt node: {node.user_prompt}")
                continue
            elif Agent.is_model_request_node(node):
                async with node.stream(agent_run.ctx) as response_stream:
                    # OLD CODE: final_result_found = False  # Removed - not needed for structured output
                    
                    async for event in response_stream:
                        if isinstance(event, PartStartEvent):
                            if isinstance(event.part, ThinkingPart):
                                logger.info("Reasoning part started (not streamed to user)")
                            elif isinstance(event.part, TextPart):
                                # logger.info(f"Text part started: {event.part.content}")
                                pass
                        elif isinstance(event, PartDeltaEvent):
                            if isinstance(event.delta, ThinkingPartDelta):
                                # Don't stream reasoning to user - just log it
                                # logger.debug(f"Reasoning delta: {event.delta.content_delta}")
                                pass
                            # OLD CODE: Text delta streaming - removed because we now use structured VoiceOutput
                            # elif isinstance(event.delta, TextPartDelta):
                            #     # Only yield text deltas after FinalResultEvent
                            #     if final_result_found and event.delta.content_delta:
                            #         yield event.delta.content_delta
                            elif isinstance(event.delta, TextPartDelta):
                                # Don't stream text deltas - we'll output structured JSON at the end
                                pass
                        elif isinstance(event, FinalResultEvent):
                            logger.info("[Result] The model started producing a final result")
                            # OLD CODE: final_result_found = True  # Removed - not needed for structured output
                            # OLD CODE: # Don't break - continue to collect text deltas
            elif Agent.is_call_tools_node(node):
                logger.info("Tool execution node")
                continue
            elif Agent.is_end_node(node):
                # Handle structured VoiceOutput
                output: VoiceOutput = node.data.output
                logger.info(f"End node reached: audio={output.audio}, end_interaction={output.end_interaction}")
                break

    # Get the result and new messages after streaming completes
    new_messages = agent_run.result.new_messages() if agent_run and agent_run.result else []
    logger.info(f"Streaming complete for session {session_id}")
    
    # OLD CODE: Just logging the output without yielding it
    # if agent_run and agent_run.result:
    #     final_output: VoiceOutput = agent_run.result.data
    #     logger.info(f"Final output - end_interaction: {final_output.end_interaction}")
    #     # You can use final_output.end_interaction here if needed for session management
    
    # Stream the structured VoiceOutput as JSON incrementally
    # This simulates OpenAI's streaming structured output behavior
    if agent_run and agent_run.result:
        final_output: VoiceOutput = agent_run.result.output
        
        # Build output dict according to spec:
        # - If end_interaction is True, include it in the output
        # - Always include audio
        output_dict = {"audio": final_output.audio}
        if final_output.end_interaction:
            output_dict["end_interaction"] = True
        
        # Convert to JSON string and stream in small chunks
        json_string = json.dumps(output_dict, ensure_ascii=False)
        
        # Stream the JSON in small chunks for proper streaming behavior
        chunk_size = 10  # Characters per chunk
        for i in range(0, len(json_string), chunk_size):
            yield json_string[i:i + chunk_size]
        
        logger.info(f"Final output sent - end_interaction: {final_output.end_interaction}")
    else:
        # Fallback if no result (shouldn't happen, but handle gracefully)
        logger.warning("No result from agent run")
        fallback_json = json.dumps({"audio": ""}, ensure_ascii=False)
        for i in range(0, len(fallback_json), 10):
            yield fallback_json[i:i + 10]
    
    # Post-processing happens AFTER streaming is complete
    messages = [
        *history,
        *new_messages
    ]

    logger.info(f"Updating message history for session {session_id} with {len(messages)} messages")
    await update_message_history(session_id, messages)