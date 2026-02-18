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
    """Get system prompt content for the given language."""
    prompt_map = {'hi': 'voice_hi', 'en': 'voice_en'}
    prompt_name = prompt_map.get(target_lang, 'voice_hi')
    return get_prompt(prompt_name, context={'today_date': get_today_date_str()})


def _create_welcome_messages(user_message: str, assistant_message: str, system_prompt: str = None, language: str = "hi") -> List[ModelMessage]:
    """Create default welcome message pair for new sessions.

    The assistant message is wrapped in VoiceOutput JSON format so the model
    sees the expected output pattern and continues producing JSON (not plain text).
    Language is omitted from the welcome message; the user has not yet chosen.
    """
    messages = []
    if system_prompt:
        messages.append(ModelRequest(parts=[SystemPromptPart(content=system_prompt)]))
    user_msg = ModelRequest(parts=[UserPromptPart(content=user_message)])
    voice_output_json = json.dumps({"audio": assistant_message, "end_interaction": False}, ensure_ascii=False)
    assistant_msg = ModelResponse(parts=[TextPart(content=voice_output_json)])
    messages.extend([user_msg, assistant_msg])
    return messages


async def _get_message_history(session_id: str, target_lang: str = "hi") -> List[ModelMessage]:
    """Get or initialize message history with welcome messages for new sessions."""
    message_history = await get_cache(f"{session_id}_{HISTORY_SUFFIX}")
    if message_history:
        return ModelMessagesTypeAdapter.validate_python(message_history)

    welcome_messages = _load_welcome_messages()
    welcome = welcome_messages.get(target_lang, welcome_messages["hi"])
    system_prompt_content = _get_system_prompt_content(target_lang)

    welcome_msg_pair = _create_welcome_messages(
        welcome["user"],
        welcome["assistant"],
        system_prompt=system_prompt_content,
        language=target_lang
    )

    await set_cache(f"{session_id}_{HISTORY_SUFFIX}", to_jsonable_python(welcome_msg_pair), ttl=DEFAULT_CACHE_TTL)
    return welcome_msg_pair

async def update_message_history(session_id: str, all_messages: List[ModelMessage]):
    """Update message history."""
    await set_cache(f"{session_id}_{HISTORY_SUFFIX}", to_jsonable_python(all_messages), ttl=DEFAULT_CACHE_TTL)

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


def group_convos(history: List[ModelMessage]) -> List[List[ModelMessage]]:
    """Group messages into conversation turns.

    Each conversation starts with a message containing a UserPromptPart.
    The first group includes everything before the second user message
    (system prompt + welcome exchange).
    """
    if not history:
        return []

    # Find indices where a new user turn starts
    user_indices = []
    for i, msg in enumerate(history):
        for part in msg.parts:
            if getattr(part, "part_kind", "") == "user-prompt":
                user_indices.append(i)
                break

    if not user_indices:
        # No user messages — return everything as one group
        return [history]

    groups = []
    # First group: everything up to (but not including) the second user message
    if len(user_indices) < 2:
        return [history]

    groups.append(history[:user_indices[1]])

    # Subsequent groups: from each user message to the next
    for idx in range(1, len(user_indices)):
        start = user_indices[idx]
        end = user_indices[idx + 1] if idx + 1 < len(user_indices) else len(history)
        groups.append(history[start:end])

    return groups


def convo_token_usage(convo: List[ModelMessage]) -> int:
    """Count total tokens in a conversation group."""
    total = 0
    for msg in convo:
        for part in msg.parts:
            total += count_tokens_for_part(part)
    return total


def trim_history(
    history: List[ModelMessage],
    max_tokens: int = 28_000,
    # include_system_prompts=True,
    # include_tool_calls=True,
) -> List[ModelMessage]:
    if not history:
        return []

    convos = group_convos(history)
    if not convos:
        return []

    # Build list of (messages, tokens)
    convo_infos = []
    for convo in convos:
        tokens = convo_token_usage(convo)
        convo_infos.append({"messages": convo, "tokens": tokens})

    # Always keep convo 0 (system + first interaction)
    first = convo_infos[0]
    rest = convo_infos[1:]

    total_tokens = first["tokens"]
    selected = []

    # Walk from newest convo backwards
    for info in reversed(rest):
        if total_tokens + info["tokens"] <= max_tokens:
            selected.insert(0, info)  # maintain chronological order
            total_tokens += info["tokens"]
        else:
            break

    final_convos = [first] + selected

    trimmed: List[ModelMessage] = []
    for info in final_convos:
        trimmed.extend(info["messages"])

    logger.info(f"Trimmed history: {total_tokens} tokens (max: {max_tokens})")

    return trimmed