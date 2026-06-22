"""
Farmer long-term memory via mem0 + Qdrant.

Three operations:
  get_profile_summary  — called at call start, returns 3-4 bullet snapshot
  search               — called by recall_farmer_context tool during call
  extract_and_save     — called post-call to persist facts from transcript
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

logger = logging.getLogger(__name__)


def _build_mem0_config() -> dict:
    from app.config import settings

    config: dict = {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": getattr(settings, "qdrant_collection", "vistaar_farmer_memories"),
                "host": getattr(settings, "qdrant_host", "localhost"),
                "port": int(getattr(settings, "qdrant_port", 6333)),
                "embedding_model_dims": 1024,
            },
        },
        "embedder": {
            "provider": "fastembed",
            "config": {"model": "intfloat/multilingual-e5-large"},
        },
    }

    inference_url = getattr(settings, "inference_endpoint_url", None)

    # Reuse the same vLLM model as the main agent for extraction
    from agents.models import LLM_AGRINET_MODEL_NAME
    extraction_model = LLM_AGRINET_MODEL_NAME or os.getenv("LLM_MODEL_NAME") or "agrinet-model"

    if inference_url:
        config["llm"] = {
            "provider": "openai",
            "config": {
                "model": extraction_model,
                "openai_base_url": inference_url,
                "api_key": getattr(settings, "inference_api_key", None) or "EMPTY",
            },
        }

    return config


def _transcript_to_mem0(history: list[ModelMessage]) -> list[dict]:
    """Convert pydantic-ai message history to mem0-compatible [{role, content}] list."""
    messages = []
    for msg in history:
        for part in msg.parts:
            kind = getattr(part, "part_kind", "")
            if kind == "user-prompt":
                messages.append({"role": "user", "content": part.content})
            elif kind == "text":
                messages.append({"role": "assistant", "content": part.content})
    return messages


class MemoryService:
    def __init__(self) -> None:
        self._client = None
        self._init_attempted = False

    def _get_client(self):
        if self._init_attempted:
            return self._client
        self._init_attempted = True
        try:
            from mem0 import Memory  # type: ignore

            self._client = Memory.from_config(_build_mem0_config())
            logger.info("MemoryService: mem0 client initialized")
        except Exception:
            logger.warning("MemoryService: initialization failed — memory disabled", exc_info=True)
        return self._client

    async def get_profile_summary(self, user_id: str) -> Optional[str]:
        """Return a 3-4 bullet profile snapshot (crop, location, language/pref) for call start."""
        client = self._get_client()
        if not client or not user_id:
            return None
        try:
            results = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.search(
                    "crop location language preference",
                    user_id=user_id,
                    limit=4,
                ),
            )
            memories = results if isinstance(results, list) else results.get("results", [])
            if not memories:
                return None
            bullets = "\n".join(f"• {m['memory']}" for m in memories[:4])
            return f"Farmer profile (summary):\n{bullets}"
        except Exception:
            logger.warning("get_profile_summary failed for user %s", user_id, exc_info=True)
            return None

    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
        threshold: float = 0.3,
    ) -> str:
        """Semantic search in Qdrant. Returns formatted string for the agent."""
        client = self._get_client()
        if not client or not user_id:
            return ""
        try:
            results = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.search(query, user_id=user_id, limit=top_k),
            )
            memories = results if isinstance(results, list) else results.get("results", [])
            if not memories:
                return "No relevant past memories found."
            filtered = [
                m for m in memories
                if m.get("score", 1.0) >= threshold
            ]
            if not filtered:
                return "No relevant past memories found."
            return "\n".join(f"- {m['memory']}" for m in filtered)
        except Exception:
            logger.warning("memory.search failed for user %s", user_id, exc_info=True)
            return ""

    async def extract_and_save(
        self,
        user_id: str,
        run_id: str,
        history: list[ModelMessage],
    ) -> None:
        """Post-call: extract facts from full transcript and persist to Qdrant."""
        client = self._get_client()
        if not client or not user_id:
            return
        messages = _transcript_to_mem0(history)
        if not messages:
            logger.info("extract_and_save: empty transcript for user %s, skipping", user_id)
            return
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.add(
                    messages,
                    user_id=user_id,
                    metadata={"run_id": run_id},
                ),
            )
            logger.info("Post-call memory saved for user %s (run %s)", user_id, run_id)
        except Exception:
            logger.error("extract_and_save failed for user %s", user_id, exc_info=True)


memory_service = MemoryService()
