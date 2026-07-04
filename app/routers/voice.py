from fastapi import APIRouter, Depends, BackgroundTasks, Request, HTTPException, status
from fastapi.responses import StreamingResponse
from app.services.voice import stream_voice_message
from app.utils import _get_message_history
from app.models.requests import ChatRequest
from app.auth.jwt_auth import decode_token_claims
from app.services.identity import user_id_from_claims, to_memory_user_id, resolve_user_id
from app.services.memory import memory_service
from app.config import settings
from app.services import call_timeout
from fastapi.security.utils import get_authorization_scheme_param
from helpers.utils import get_logger
import uuid
import asyncio
from typing import AsyncGenerator

logger = get_logger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])

_session_locks: dict[str, asyncio.Lock] = {}


def _get_session_lock(session_id: str) -> asyncio.Lock:
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


async def _locked_stream(
    lock: asyncio.Lock,
    generator: AsyncGenerator[str, None],
    session_id: str,
    memory_user_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Hold the session lock for the entire lifetime of the stream.

    When the turn finishes, (re)start the inactivity timer so the call is saved
    automatically if no further turn arrives (demo/POC fallback for call-ended).
    """
    async with lock:
        try:
            async for chunk in generator:
                yield chunk
        finally:
            call_timeout.schedule(session_id, memory_user_id)


def _resolve_memory_user_id(http_request: Request, request: ChatRequest) -> str | None:
    """Resolve the hashed memory user_id from the request.

    Production: the farmer's phone MUST come from a verified JWT (claim pinned by
    JWT_PHONE_CLAIM, default `sub`); the phone is hashed here so raw phone never
    reaches Qdrant. A missing/invalid token or absent phone raises 401.

    Development: if no valid JWT phone is present, fall back to the `user_id`
    query param so local testing works without signing tokens.
    """
    scheme, token = get_authorization_scheme_param(http_request.headers.get("Authorization"))
    if token and scheme.lower() == "bearer":
        hashed = user_id_from_claims(decode_token_claims(token) or {})
        if hashed:
            return hashed

    if settings.environment == "development":
        # Dev/testing fallback: hash a phone, or use an opaque id as-is.
        return to_memory_user_id(request.user_id)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="A valid Bearer token containing the farmer phone is required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.get("/memories")
async def list_memories(phone: str):
    """Read-only: return all long-term memories for a farmer, looked up by phone.

    The phone is hashed here (same logic as call-time) so the raw number never
    leaves this process and the caller (the memory viewer UI) needs no secrets.
    Intended for internal POC/demo use — unauthenticated.
    """
    user_id = resolve_user_id(phone)
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid phone number")
    items = await memory_service.get_all(user_id)
    return {"user_id": user_id, "count": len(items), "memories": items}


@router.delete("/memories")
async def delete_memories(phone: str):
    """Read-write: delete all long-term memories for a farmer, looked up by phone.

    Phone is hashed here (same as call-time). Intended for internal POC/demo
    cleanup — unauthenticated.
    """
    user_id = resolve_user_id(phone)
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid phone number")
    deleted = await memory_service.delete_all(user_id)
    return {"user_id": user_id, "deleted": deleted}


@router.get("/profile")
async def get_profile(phone: str):
    """Read-only: return the structured farmer profile, looked up by phone.

    Phone is hashed here (same as call-time). Internal POC/demo use.
    """
    user_id = resolve_user_id(phone)
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid phone number")
    from app.services.profile import profile_store
    profile = await profile_store.get(user_id)
    return {"user_id": user_id, "profile": profile.model_dump() if profile else None}


@router.delete("/profile")
async def delete_profile(phone: str):
    """Delete the structured farmer profile, looked up by phone. POC/demo cleanup."""
    user_id = resolve_user_id(phone)
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid phone number")
    from app.services.profile import profile_store
    deleted = await profile_store.delete(user_id)
    return {"user_id": user_id, "deleted": deleted}


@router.get("/")
async def voice_endpoint(
    http_request: Request,
#    background_tasks: BackgroundTasks,
    request: ChatRequest = Depends(),
):
    """
    Chat endpoint that streams responses back to the client.

    Auth: enforced in production (JWT Bearer with the farmer phone in the
    `sub` claim); relaxed in development to allow the `user_id` query param.
    """
    session_id = request.session_id or str(uuid.uuid4())
    memory_user_id = _resolve_memory_user_id(http_request, request)

    # New turn arrived — cancel any pending inactivity-based call-end save.
    call_timeout.cancel(session_id)

    logger.info(
        f"Chat request received - session_id: {session_id}, user_id: {request.user_id}, "
        f"source_lang: {request.source_lang}, target_lang: {request.target_lang}, "
        f"provider: {request.provider}, process_id: {request.process_id}, query: {request.query}"
    )
    
    lock = _get_session_lock(session_id)
    if lock.locked():
        logger.info(f"Request queued for session {session_id} (another request in progress)")
    
    history = await _get_message_history(session_id, target_lang=request.target_lang)
    logger.debug(f"Retrieved message history for session {session_id} - length: {len(history)}")
        
    return StreamingResponse(
        _locked_stream(
            lock,
            stream_voice_message(
                query=request.query,
                session_id=session_id,
                source_lang=request.source_lang,
                target_lang=request.target_lang,
                history=history,
                provider=request.provider,
                process_id=request.process_id,
                user_id=memory_user_id,
            ),
            session_id,
            memory_user_id,
        ),
        media_type='text/plain; charset=utf-8',
        headers={
            # Tell nginx/ingress not to buffer the stream — buffering upstream
            # turns real token streaming into one big delayed chunk.
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )
