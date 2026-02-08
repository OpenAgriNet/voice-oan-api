from typing import List, Dict
import json
import os
from app.core.cache import cache  # Import cache instance from core
from helpers.utils import get_logger, count_tokens_for_part, get_prompt, get_today_date_str
from copy import deepcopy
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelMessage,
    SystemPromptPart,
    UserPromptPart,
    TextPart,
    ModelRequest,
    ModelResponse,
)
from pydantic_core import to_jsonable_python

HISTORY_SUFFIX = "_SVA"

DEFAULT_CACHE_TTL = 60*60*24 # 24 hours

logger = get_logger(__name__)

# Load welcome messages from assets file
def _load_welcome_messages() -> Dict[str, Dict[str, str]]:
    """Load welcome messages from assets/welcome_messages.json."""
    file_path = os.path.join(os.path.dirname(__file__), "..", "assets", "welcome_messages.json")
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)

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


def _get_system_prompt_content(target_lang: str = "hi") -> str:
    """Get system prompt content for the given language.
    
    Args:
        target_lang: Target language code (default: "hi")
        
    Returns:
        System prompt content as string
    """
    # Map language codes to prompt file names (same as in agents/voice.py)
    prompt_map = {
        'hi': 'voice_hi',
        'en': 'voice_en'
    }
    
    # Default to 'voice_hi' if language not in map
    prompt_name = prompt_map.get(target_lang, 'voice_hi')
    return get_prompt(prompt_name, context={'today_date': get_today_date_str()})


def _create_welcome_messages(user_message: str, assistant_message: str, system_prompt: str = None) -> List[ModelMessage]:
    """Create default welcome message pair for new sessions, optionally with system prompt.
    
    Args:
        user_message: The default user message content
        assistant_message: The default assistant message content
        system_prompt: Optional system prompt content to include before welcome messages
        
    Returns:
        List containing system prompt (if provided), user and assistant ModelMessage objects
    """
    messages = []
    
    # Add system prompt if provided
    if system_prompt:
        system_msg = ModelRequest(parts=[SystemPromptPart(content=system_prompt)])
        messages.append(system_msg)
    
    # Add welcome user and assistant messages
    user_msg = ModelRequest(parts=[UserPromptPart(content=user_message)])
    assistant_msg = ModelResponse(parts=[TextPart(content=assistant_message)])
    messages.extend([user_msg, assistant_msg])
    
    return messages


async def _get_message_history(session_id: str, target_lang: str = "hi") -> List[ModelMessage]:
    """Get or initialize message history with welcome messages for new sessions.
    
    Args:
        session_id: Session identifier
        target_lang: Target language code for welcome messages (default: "hi")
        
    Returns:
        List of ModelMessage objects, including welcome messages for new sessions
    """
    message_history = await get_cache(f"{session_id}_{HISTORY_SUFFIX}")
    if message_history:
        return ModelMessagesTypeAdapter.validate_python(message_history)
    
    # New session - create welcome messages
    # Load welcome messages from assets file
    welcome_messages = _load_welcome_messages()
    
    # Get welcome messages for the target language, default to Hindi
    welcome = welcome_messages.get(target_lang, welcome_messages["hi"])
    
    # Get system prompt content for the target language
    system_prompt_content = _get_system_prompt_content(target_lang)
    
    # Create welcome messages with system prompt
    welcome_msg_pair = _create_welcome_messages(
        welcome["user"], 
        welcome["assistant"],
        system_prompt=system_prompt_content
    )
    
    # Save welcome messages (with system prompt) to cache so they persist
    await set_cache(f"{session_id}_{HISTORY_SUFFIX}", to_jsonable_python(welcome_msg_pair), ttl=DEFAULT_CACHE_TTL)
    
    return welcome_msg_pair

async def _get_moderation_history(session_id: str) -> List[ModelMessage]:
    """Get or initialize moderation history."""
    moderation_history = await get_cache(f"{session_id}_{HISTORY_SUFFIX}_MODERATION")
    if moderation_history:
        return ModelMessagesTypeAdapter.validate_python(moderation_history)
    return []

async def update_message_history(session_id: str, all_messages: List[ModelMessage]):
    """Update message history."""
    await set_cache(f"{session_id}_{HISTORY_SUFFIX}", to_jsonable_python(all_messages), ttl=DEFAULT_CACHE_TTL)

async def update_moderation_history(session_id: str, moderation_messages: List[ModelMessage]):
    """Update moderation history."""
    await set_cache(f"{session_id}_{HISTORY_SUFFIX}_MODERATION", to_jsonable_python(moderation_messages), ttl=DEFAULT_CACHE_TTL)

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