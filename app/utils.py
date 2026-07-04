import os
from typing import List

from app.core.cache import cache  # Import cache instance from core
from copy import deepcopy
from helpers.utils import get_logger, count_tokens_for_part, get_prompt, get_today_date_str
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_core import to_jsonable_python

HISTORY_SUFFIX = "_SVA"

# Session-state TTL. A voice call lives minutes, not days: the history only
# needs to survive the active call plus gaps between turns. Cross-call
# continuity comes from Qdrant (profile + memories), not this cache — and a
# long TTL risks resuming a stale conversation if a provider reuses session_id.
DEFAULT_CACHE_TTL = int(os.getenv("SESSION_CACHE_TTL_SECONDS", str(2 * 60 * 60)))  # 2 hours

logger = get_logger(__name__)

# Cache utility functions
async def get_cache(key: str):
    """
    Get value from cache.
    
    Args:
        key: Cache key to retrieve
        
    Returns:
        Cached value or None if not found
    """
    return await cache.get(key)


async def set_cache(key: str, value, ttl: int = DEFAULT_CACHE_TTL):
    """
    Set value in cache with TTL.
    
    Args:
        key: Cache key to store under
        value: Value to cache (will be JSON serialized)
        ttl: Time to live in seconds (default: 24 hours)
        
    Returns:
        True if successful
    """
    await cache.set(key, value, ttl=ttl)
    return True


def _normalize_voice_system_lang(target_lang: str | None) -> str:
    lang = (target_lang or "mr").lower()
    if lang in ("en", "hi", "mr"):
        return lang
    return "mr"


def _get_system_prompt_content(target_lang: str = "mr") -> str:
    lang = _normalize_voice_system_lang(target_lang)
    return get_prompt(f"voice_system_{lang}", context={"today_date": get_today_date_str()})


# Identity / "name and age?" scripted reply — keep in sync with assets/prompts/voice_system_{en,hi,mr}.md
_WELCOME_ASSISTANT_TEXT: dict[str, str] = {
    "en": (
        "My name is Vasudha. I am a digital assistant built to help farmers with farming "
        "information. How can I help you?"
    ),
    "hi": (
        "मेरा नाम वसुधा है। मैं एक डिजिटल सहायक हूँ, जो किसानों को कृषि संबंधी जानकारी और प्रश्नों में "
        "मदद करने के लिए बनाई गई हूँ। कृपया बताइए, मैं आपकी किस तरह मदद कर सकती हूँ?"
    ),
    "mr": (
        "माझं नाव वसुधा आहे. मी एक डिजिटल सहाय्यक आहे, शेतकऱ्यांना शेतीविषयक माहिती देण्यासाठी तयार. "
        "कृपया सांगा, मी तुम्हाला कशात मदत करू?"
    ),
}


def _welcome_assistant_message(target_lang: str) -> str:
    lang = _normalize_voice_system_lang(target_lang)
    return _WELCOME_ASSISTANT_TEXT.get(lang, _WELCOME_ASSISTANT_TEXT["mr"])


def _default_user_greeting(target_lang: str) -> str:
    lang = _normalize_voice_system_lang(target_lang)
    return {"en": "Hello", "hi": "नमस्ते", "mr": "नमस्कार"}.get(lang, "नमस्कार")


def _create_welcome_messages(
    user_message: str,
    assistant_message: str,
    *,
    system_prompt: str | None = None,
) -> List[ModelMessage]:
    messages: List[ModelMessage] = []
    if system_prompt:
        messages.append(ModelRequest(parts=[SystemPromptPart(content=system_prompt)]))
    messages.extend(
        [
            ModelRequest(parts=[UserPromptPart(content=user_message)]),
            ModelResponse(parts=[TextPart(content=assistant_message)]),
        ]
    )
    return messages


async def _get_message_history(
    session_id: str,
    target_lang: str = "mr",
) -> List[ModelMessage]:
    """Load cached history or seed a new session with system prompt and one welcome turn."""
    message_history = await get_cache(f"{session_id}_{HISTORY_SUFFIX}")
    if message_history:
        return ModelMessagesTypeAdapter.validate_python(message_history)

    system_prompt_content = _get_system_prompt_content(target_lang)
    assistant_opening = _welcome_assistant_message(target_lang)
    welcome_pair = _create_welcome_messages(
        _default_user_greeting(target_lang),
        assistant_opening,
        system_prompt=system_prompt_content,
    )
    await set_cache(
        f"{session_id}_{HISTORY_SUFFIX}",
        to_jsonable_python(welcome_pair),
        ttl=DEFAULT_CACHE_TTL,
    )
    return welcome_pair


async def update_message_history(session_id: str, all_messages: List[ModelMessage]):
    """Persist the full message list for a session."""
    await set_cache(f"{session_id}_{HISTORY_SUFFIX}", to_jsonable_python(all_messages), ttl=DEFAULT_CACHE_TTL)


def inject_profile_into_history(history: List[ModelMessage], snapshot: str) -> List[ModelMessage]:
    """Append the farmer-profile snapshot to the session's cached system prompt.

    The session history is seeded with a *static* SystemPromptPart (no
    dynamic_ref), and pydantic-ai skips the agent's system-prompt functions
    whenever message_history is non-empty — so a snapshot loaded into
    deps.user_memories never reaches the model on its own. Mutating the cached
    system prompt (and persisting it) is the only path that puts the profile in
    front of the model for every turn of the call.

    Appending at the END of the system prompt keeps the shared base prompt a
    byte-identical prefix across sessions, so vLLM prefix caching still hits.
    Idempotent: re-injecting the same snapshot is a no-op.
    """
    for msg in history:
        if isinstance(msg, ModelRequest):
            for i, part in enumerate(msg.parts):
                if isinstance(part, SystemPromptPart):
                    if snapshot not in part.content:
                        msg.parts[i] = SystemPromptPart(content=f"{part.content}\n\n{snapshot}")
                    return history
    history.insert(0, ModelRequest(parts=[SystemPromptPart(content=snapshot)]))
    return history

def filter_out_tool_calls(messages: List[ModelMessage]) -> List[ModelMessage]:
    """Filter out tool calls and tool returns from the message history.
    
    Args:
        messages: List of messages (ModelRequest/ModelResponse objects)
        
    Returns:
        List of messages with tool calls and returns removed
    """
    if not messages:
        return []
    
    filtered_messages = []
    for message in messages:
        # Create a deep copy to avoid modifying the original
        msg_copy = deepcopy(message)
        filtered_parts = []
        
        for part in msg_copy.parts:
            # Only keep non-tool parts
            if not hasattr(part, 'part_kind') or part.part_kind not in ['tool-call', 'tool-return']:
                filtered_parts.append(part)
        
        # Only add messages that have non-tool parts
        if filtered_parts:
            msg_copy.parts = filtered_parts
            filtered_messages.append(msg_copy)            
    return filtered_messages



def get_message_pairs(history: List[ModelMessage], limit: int = None) -> List[List]:
    """Extract user/assistant message part pairs from history, starting with the most recent.
    
    Args:
        history: List of messages (ModelMessage objects)
        limit: Maximum number of message pairs to return (None = all pairs)
        
    Returns:
        List of [UserPromptPart, TextPart] pairs, starting with the most recent
    """
    if not history:
        return []
    
    pairs = []
    # Process messages in reverse chronological order (newest first)
    i = len(history) - 1
    
    while i > 0 and (limit is None or len(pairs) < limit):
        # Find the nearest assistant message (with 'text' part)
        assistant_idx = None
        text_part = None
        for j in range(i, -1, -1):
            # Find the TextPart in the message
            for part in history[j].parts:
                if getattr(part, "part_kind", "") == "text":
                    assistant_idx = j
                    text_part = part
                    break
            if assistant_idx is not None:
                break
        
        if assistant_idx is None or text_part is None:
            break  # No more assistant messages
            
        # Find the nearest user message before the assistant message
        user_idx = None
        user_part = None
        for j in range(assistant_idx - 1, -1, -1):
            # Find the UserPromptPart in the message
            for part in history[j].parts:
                if getattr(part, "part_kind", "") == "user-prompt":
                    user_idx = j
                    user_part = part
                    break
            if user_idx is not None:
                break
                
        if user_idx is None or user_part is None:
            break  # No more user messages
            
        # Add the pair and continue searching from before this pair
        pairs.append([deepcopy(user_part), deepcopy(text_part)])
        i = user_idx - 1
        
    return pairs

def format_message_pairs(history: List[ModelMessage], limit: int = None) -> List[str]:
    """Format user/assistant message pairs as strings with custom headers.
    
    Args:
        history: List of messages (ModelMessage objects)
        limit: Maximum number of message pairs to return (None = all pairs)
        
    Returns:
        List of formatted strings containing user and assistant messages
    """
    pairs = get_message_pairs(history, limit)
    formatted_messages = []
    
    for user_part, assistant_part in pairs:
        formatted_pair = f"""**User Message**:\n{user_part.content}\n\n**Assistant Message**:\n{assistant_part.content}"""
        formatted_messages.append(formatted_pair)
    
    return formatted_messages


def clean_message_history_for_openai(history: List[ModelMessage]) -> List[ModelMessage]:
    """Clean message history to ensure it's safe for OpenAI API.
    
    Removes orphaned tool calls (tool calls without responses) from the message history
    to prevent OpenAI API errors. Processes messages in order and removes any tool call 
    parts that don't have corresponding tool response parts.
    
    Args:
        history: List of messages to clean
        
    Returns:
        Cleaned list of messages safe for OpenAI API
    """
    if not history:
        return []
    
    logger.debug(f"Cleaning message history with {len(history)} messages")
    
    # First pass: collect all tool call IDs and their corresponding responses
    tool_calls = set()
    tool_responses = set()
    
    for message in history:
        for part in message.parts:
            part_kind = getattr(part, "part_kind", "")
            tool_call_id = getattr(part, "tool_call_id", None)
            
            if not tool_call_id:
                continue
                
            if part_kind == "tool-call":
                tool_calls.add(tool_call_id)
            elif part_kind in ("tool-return", "retry-prompt"):
                tool_responses.add(tool_call_id)
    
    # Identify orphaned tool calls (calls without responses)
    orphaned_calls = tool_calls - tool_responses
    
    # Second pass: filter out orphaned tool calls and their responses
    cleaned_history = []
    
    for message in history:
        cleaned_parts = []
        
        for part in message.parts:
            part_kind = getattr(part, "part_kind", "")
            tool_call_id = getattr(part, "tool_call_id", None)
            
            # Skip orphaned tool calls
            if part_kind == "tool-call" and tool_call_id in orphaned_calls:
                logger.debug(f"Removing orphaned tool call: {tool_call_id}")
                continue
            
            # Skip responses to orphaned tool calls
            if part_kind in ("tool-return", "retry-prompt") and tool_call_id in orphaned_calls:
                logger.debug(f"Removing response to orphaned tool call: {tool_call_id}")
                continue
            
            cleaned_parts.append(part)
        
        # Only keep messages with remaining parts
        if cleaned_parts:
            cleaned_message = deepcopy(message)
            cleaned_message.parts = cleaned_parts
            cleaned_history.append(cleaned_message)
    
    if orphaned_calls:
        logger.warning(f"Removed {len(orphaned_calls)} orphaned tool calls: {orphaned_calls}")
    
    logger.info(f"Cleaned message history: {len(history)} -> {len(cleaned_history)} messages")
    return cleaned_history


def trim_history(
    history: List[ModelMessage],
    max_tokens: int = 28_000,
    *,
    include_system_prompts: bool = True,
    include_tool_calls: bool = True,
) -> List[ModelMessage]:
    # 1. Pre-process system parts: strip them or keep whole messages
    prepped: List[ModelMessage] = []
    for msg in history:
        if include_system_prompts:
            prepped.append(msg)
        else:
            # remove only the system parts, keep any other parts (like user-prompt)
            new_parts = [p for p in msg.parts if not isinstance(p, SystemPromptPart)]
            if new_parts:
                m2 = deepcopy(msg)
                m2.parts = new_parts
                prepped.append(m2)

    # 2. Split into "turns" at each user message
    turns: List[List[ModelMessage]] = []
    current: List[ModelMessage] = []
    for msg in prepped:
        is_user = any(getattr(p, "part_kind", "") == "user-prompt" for p in msg.parts)
        if is_user and current:
            turns.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        turns.append(current)

    # 3. Globally identify all paired tool calls/returns/retries across all turns
    # This prevents orphaned tool calls/returns/retries from being retained
    all_calls = set()
    all_returns = set()
    all_retries = set()
    
    # First pass: collect all tool call, return, and retry IDs globally across entire history
    for turn in turns:
        for m in turn:
            for p in m.parts:
                kind = getattr(p, "part_kind", "")
                if kind == "tool-call" and hasattr(p, 'tool_call_id'):
                    all_calls.add(p.tool_call_id)
                elif kind == "tool-return" and hasattr(p, 'tool_call_id'):
                    all_returns.add(p.tool_call_id)
                elif kind == "retry-prompt" and hasattr(p, 'tool_call_id'):
                    all_retries.add(p.tool_call_id)
    
    # Keep tool calls that have either a return OR a retry (both are valid responses)
    good_ids = all_calls & (all_returns | all_retries)
    
    # 4. Filter each turn using the global good_ids to remove orphaned tool calls/returns/retries
    clean_turns: List[List[ModelMessage]] = []
    for turn in turns:
        filtered: List[ModelMessage] = []
        for m in turn:
            kept = []
            for p in m.parts:
                # drop any part with an empty 'content' attribute
                if hasattr(p, "content") and not getattr(p, "content"):
                    continue
                kind = getattr(p, "part_kind", "")
                if kind in ("tool-call", "tool-return", "retry-prompt"):
                    # Use global good_ids to filter out orphaned tool calls/returns/retries
                    if not include_tool_calls or not hasattr(p, 'tool_call_id') or p.tool_call_id not in good_ids:
                        continue
                kept.append(p)
            if kept:
                m2 = deepcopy(m)
                m2.parts = kept
                filtered.append(m2)
        if filtered:
            clean_turns.append(filtered)

    # 5. Compute token-count per turn
    turn_tokens = [
        sum(count_tokens_for_part(p) for m in t for p in m.parts)
        for t in clean_turns
    ]

    # 6. Identify system turn and calculate its token usage
    system_turn = None
    system_turn_tokens = 0
    
    if include_system_prompts:
        # Find the first turn with system prompt parts
        for i, turn in enumerate(clean_turns):
            # First, check if this turn actually has system prompt parts
            has_system_part = any(
                isinstance(p, SystemPromptPart) 
                for m in turn 
                for p in m.parts
            )
            if has_system_part:
                system_turn = turn
                system_turn_tokens = turn_tokens[i]
                # Remove this turn from clean_turns and turn_tokens
                clean_turns = clean_turns[:i] + clean_turns[i+1:]
                turn_tokens = turn_tokens[:i] + turn_tokens[i+1:]
                break
    
    # 7. Greedily pick most-recent turns until we hit max_tokens
    remaining_tokens = max_tokens
    
    # Reduce remaining tokens if we have a system turn to include
    if system_turn is not None:
        remaining_tokens -= system_turn_tokens
        # Make sure we don't go negative
        remaining_tokens = max(0, remaining_tokens)
    
    # Select recent turns that fit in remaining token budget
    selected_turns = []
    total_tokens = 0
    
    for turn, tk in zip(reversed(clean_turns), reversed(turn_tokens)):
        if total_tokens + tk <= remaining_tokens:
            selected_turns.insert(0, turn)
            total_tokens += tk
        else:
            break
    
    # 8. Combine system turn (if any) with selected recent turns
    final_turns = []
    if system_turn is not None:
        final_turns.append(system_turn)
    final_turns.extend(selected_turns)
    
    # 9. Flatten into a single list
    trimmed = [msg for turn in final_turns for msg in turn if msg.parts]
    return trimmed