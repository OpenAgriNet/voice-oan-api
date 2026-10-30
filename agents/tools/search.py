"""
Marqo client implementation for vector search + network scheme document search.
"""
import asyncio
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

import httpx
import marqo
from pydantic import BaseModel, Field
from pydantic_ai import ModelRetry
from pydantic_ai.tools import RunContext
from helpers.utils import get_logger
from agents.deps import FarmerContext
from agents.tools.terms import normalize_text_with_glossary
from app.config import get_default_httpx_timeout
from app.observability.langfuse_client import safe_update_current_span
from helpers.scheme_qdrant_search import (
    format_qdrant_scheme_codes_for_doc,
    format_scheme_unavailable,
    format_search_results,
    format_status_check_offer,
    get_builtin_scheme_list,
    query_names_unindexed_scheme,
    resolve_scheme_code,
)
from langfuse import observe

logger = get_logger(__name__)

DocumentType = Literal['video', 'document']

SCHEME_AGRI_QDRANT_CATEGORY = "scheme-agri-qdrant"

class SearchHit(BaseModel):
    """Individual search hit from elasticsearch"""
    name: str
    text: str
    doc_id: str
    type: str
    source: str
    score: float = Field(alias="_score")
    id: str = Field(alias="_id")

    @property
    def processed_text(self) -> str:
        """Returns the text with cleaned up whitespace and newlines"""
        # Replace multiple newlines with a single line
        cleaned = re.sub(r'\n{2,}', '\n\n', self.text)
        cleaned = re.sub(r'\t+', '\t', cleaned)
        cleaned = normalize_text_with_glossary(cleaned)
        return cleaned

    def __str__(self) -> str:
        if self.type == 'document':
            return f"**{self.name}**\n" + "```\n" + self.processed_text +  "\n```\n"
        else:
            return f"**[{self.name}]({self.source})**\n" + "```\n" + self.processed_text + "\n```\n"


def _tag_list_to_map(entries: Any) -> Dict[str, str]:
    """Flatten Beckn tag list entries to {descriptor.code: value}."""
    out: Dict[str, str] = {}
    if not isinstance(entries, list):
        return out
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        desc = ent.get("descriptor") if isinstance(ent.get("descriptor"), dict) else {}
        code = str(desc.get("code") or "").strip().lower().replace("-", "_")
        if not code:
            continue
        value = ent.get("value")
        if value is None:
            continue
        out[code] = str(value)
    return out


def _catalog_search_context_map(catalog: Dict[str, Any]) -> Dict[str, str]:
    """Parse catalog.tags search-context group (status, resolved-scheme-code, source, …)."""
    tags = catalog.get("tags")
    if not isinstance(tags, list):
        return {}
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        desc = tag.get("descriptor") if isinstance(tag.get("descriptor"), dict) else {}
        code = str(desc.get("code") or "").strip().lower().replace("-", "_")
        name = str(desc.get("name") or "").strip().lower()
        if code != "search_context" and name != "search context":
            continue
        return _tag_list_to_map(tag.get("list"))
    return {}


def _item_chunk_details_map(item: Dict[str, Any]) -> Dict[str, str]:
    """Parse item.tags chunk-details group into a flat map."""
    tags = item.get("tags")
    if not isinstance(tags, list):
        return {}
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        desc = tag.get("descriptor") if isinstance(tag.get("descriptor"), dict) else {}
        code = str(desc.get("code") or "").strip().lower().replace("-", "_")
        name = str(desc.get("name") or "").strip().lower()
        if code != "chunk_details" and name != "chunk details":
            continue
        return _tag_list_to_map(tag.get("list"))
    return {}


def _chunk_result_from_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map one Beckn catalog item to the format expected by format_search_results."""
    if not isinstance(item, dict):
        return None
    details = _item_chunk_details_map(item)
    desc = item.get("descriptor") if isinstance(item.get("descriptor"), dict) else {}

    text = (details.get("text") or desc.get("long_desc") or "").strip()
    if not text:
        return None

    scheme_code = (
        details.get("scheme_code")
        or desc.get("code")
        or ""
    ).strip()
    scheme_name = (
        details.get("scheme_name")
        or desc.get("name")
        or ""
    ).strip()
    section = (details.get("section") or "").strip().lower() or "other"
    doc_id = (details.get("doc_id") or "").strip()
    chunk_id = (details.get("chunk_id") or item.get("id") or "").strip()
    source = (details.get("source") or "").strip()

    score_raw = details.get("score") or "0"
    try:
        score = float(score_raw)
    except (TypeError, ValueError):
        score = 0.0

    return {
        "score": score,
        "scheme_code": scheme_code,
        "scheme_name": scheme_name,
        "text": text,
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "section": section,
        "source": source,
    }


def _parse_scheme_network_response(data: Dict[str, Any]) -> tuple[List[Dict[str, Any]], Dict[str, str]]:
    """
    Parse BAP client /search response for scheme-agri-qdrant.

    Returns (chunk_results, search_context) from the first successful catalog
    (or the first catalog when status is absent).
    """
    responses = data.get("responses") if isinstance(data, dict) else None
    if not isinstance(responses, list) or not responses:
        return [], {}

    selected_ctx: Dict[str, str] = {}
    selected_results: List[Dict[str, Any]] = []

    for rsp in responses:
        if not isinstance(rsp, dict):
            continue
        message = rsp.get("message") if isinstance(rsp.get("message"), dict) else {}
        catalog = message.get("catalog") if isinstance(message.get("catalog"), dict) else {}
        if not catalog:
            continue

        ctx = _catalog_search_context_map(catalog)
        status = str(ctx.get("status") or "").strip().lower()
        # Prefer a successful catalog when multiple responses exist.
        if status and status != "success" and selected_results:
            continue

        results: List[Dict[str, Any]] = []
        providers = catalog.get("providers")
        if isinstance(providers, list):
            for prov in providers:
                if not isinstance(prov, dict):
                    continue
                items = prov.get("items")
                if not isinstance(items, list):
                    continue
                for item in items:
                    parsed = _chunk_result_from_item(item)
                    if parsed:
                        results.append(parsed)

        if status == "success" or (not status and results):
            return results, ctx

        # Keep first non-empty / first catalog as fallback.
        if not selected_ctx:
            selected_ctx = ctx
            selected_results = results
        elif results and not selected_results:
            selected_ctx = ctx
            selected_results = results

    return selected_results, selected_ctx


def _build_scheme_qdrant_search_payload(
    query: str,
    scheme_code: Optional[str] = None,
    session_id: str = "",
    question_id: str = "",
) -> Dict[str, Any]:
    """Beckn /search body for scheme guideline document search (scheme-agri-qdrant)."""
    now = datetime.now(timezone.utc)
    item_descriptor: Dict[str, str] = {"name": query}
    if scheme_code:
        item_descriptor["code"] = scheme_code

    return {
        "context": {
            "domain": "schemes:vistaar",
            "action": "search",
            "version": "1.1.0",
            "bap_id": os.getenv("BAP_ID"),
            "bap_uri": os.getenv("BAP_URI"),
            "bpp_id": os.getenv("BPP_ID"),
            "bpp_uri": os.getenv("BPP_URI"),
            "transaction_id": str(uuid.uuid4()),
            "message_id": str(uuid.uuid4()),
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "ttl": "PT10M",
            "location": {
                "country": {"code": "IND"},
                "city": {"code": "*"},
            },
            "tags": {
                "session_id": session_id,
                "question_id": question_id,
            },
        },
        "message": {
            "intent": {
                "category": {
                    "descriptor": {
                        "code": SCHEME_AGRI_QDRANT_CATEGORY,
                        "name": SCHEME_AGRI_QDRANT_CATEGORY,
                    }
                },
                "item": {
                    "descriptor": item_descriptor,
                },
            }
        },
    }


@observe(name="tool:search_schemes", as_type="tool")
async def search_schemes(
    ctx: RunContext[FarmerContext],
    query: str,
    top_k: int = 10,
) -> str:
    """
    Semantic search for Bharat Vistaar scheme guideline PDFs via the Vistaar network layer
    (BAP /search, category scheme-agri-qdrant).

    PLACEHOLDER_SCHEME_CODES

    Use for every scheme routed through the live catalog. MahaVistaar and
    AmulVistaar cross-network schemes use their dedicated tools instead.
    For a direct status/grievance request (e.g. "check my PM-KISAN status"), call the
    status tools instead — do not call this first.

    Args:
        query: Natural-language question in English (eligibility, benefits, application process)
        top_k: Maximum number of chunks to return (default: 10)

    Returns:
        Formatted scheme document chunks with scheme names and relevance scores
    """
    scheme_list = get_builtin_scheme_list()
    logger.info(
        "TOOL CALL: search_schemes(query=%r, top_k=%d) — %d schemes known this turn (live-synced from Redis)",
        query, top_k, len(scheme_list),
    )
    if query_names_unindexed_scheme(query, scheme_list):
        logger.info("TOOL RESULT: search_schemes — query names a scheme outside the known list, returning unavailable")
        return format_scheme_unavailable(query)

    scheme_code = resolve_scheme_code(query, scheme_list)
    logger.info("search_schemes: resolved scheme_code=%s for query=%r", scheme_code, query)
    payload = _build_scheme_qdrant_search_payload(
        query=query,
        scheme_code=scheme_code,
        session_id=ctx.deps.session_id,
        question_id=ctx.deps.question_id,
    )
    transaction_id = payload.get("context", {}).get("transaction_id")
    safe_update_current_span(
        metadata={
            "tool": "scheme.search",
            "query": query,
            "top_k": top_k,
            "category": SCHEME_AGRI_QDRANT_CATEGORY,
            "transaction_id": transaction_id,
            "resolved_scheme_code": scheme_code,
            "known_scheme_count": len(scheme_list),
        }
    )

    bap_endpoint = os.getenv("BAP_ENDPOINT")
    if not bap_endpoint:
        logger.error("BAP_ENDPOINT is not set")
        return "Scheme search service is not configured. Please try again later."

    search_url = bap_endpoint.rstrip("/") + "/search"
    logger.info(
        "Scheme network search: query=%r scheme_code=%s url=%s",
        query,
        scheme_code,
        search_url,
    )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                search_url,
                json=payload,
                timeout=get_default_httpx_timeout(),
            )

        if response.status_code != 200:
            logger.error(
                "Scheme network search returned status %s — %s",
                response.status_code,
                (response.text or "")[:500],
            )
            safe_update_current_span(
                metadata={
                    "tool": "scheme.search",
                    "http_status": int(response.status_code),
                }
            )
            return "Scheme search service is unavailable. Please try again later."

        data = response.json()
        if not isinstance(data, dict):
            logger.error("Scheme network search returned non-object JSON: %s", type(data))
            return "Scheme search returned an unexpected response. Please try again later."

        results, search_ctx = _parse_scheme_network_response(data)
        status = str(search_ctx.get("status") or "").strip().lower()
        message = str(search_ctx.get("message") or "").strip()
        resolved = str(search_ctx.get("resolved_scheme_code") or "").strip()

        if status and status != "success" and not results:
            logger.info(
                "TOOL RESULT: search_schemes — network non-success status=%s message=%r hits=%s",
                status,
                message,
                len(results),
            )
            safe_update_current_span(
                metadata={
                    "tool": "scheme.search",
                    "category": SCHEME_AGRI_QDRANT_CATEGORY,
                    "transaction_id": transaction_id,
                    "network_status": status or None,
                    "resolved_scheme_code": resolved or scheme_code,
                    "hit_count": 0,
                    "doc_ids": [],
                    "chunk_ids": [],
                    "providers_source": search_ctx.get("source"),
                    "search_backend": search_ctx.get("search_backend"),
                }
            )
            return message or format_scheme_unavailable(query)

        if top_k and top_k > 0:
            results = results[:top_k]

        # From network chunk-details tags — shown in Langfuse tool metadata.
        doc_ids = list(
            dict.fromkeys(str(r.get("doc_id") or "") for r in results if r.get("doc_id"))
        )
        chunk_ids = [str(r.get("chunk_id") or "") for r in results if r.get("chunk_id")]

        safe_update_current_span(
            metadata={
                "tool": "scheme.search",
                "category": SCHEME_AGRI_QDRANT_CATEGORY,
                "transaction_id": transaction_id,
                "network_status": status or None,
                "resolved_scheme_code": resolved or scheme_code,
                "hit_count": len(results),
                "doc_ids": doc_ids,
                "chunk_ids": chunk_ids,
                "chunks": [
                    {
                        "doc_id": r.get("doc_id") or "",
                        "chunk_id": r.get("chunk_id") or "",
                        "scheme_code": r.get("scheme_code") or "",
                        "section": r.get("section") or "",
                        "score": r.get("score"),
                        "source": r.get("source") or "",
                    }
                    for r in results
                ],
                "providers_source": search_ctx.get("source"),
                "search_backend": search_ctx.get("search_backend"),
            }
        )
        logger.info(
            "TOOL RESULT: search_schemes — resolved_scheme_code=%s hit_count=%d doc_ids=%s",
            resolved or scheme_code, len(results), doc_ids,
        )

        formatted = format_search_results(results, query, scheme_list)
        if results:
            formatted += format_status_check_offer(resolved or scheme_code)
        return formatted

    except httpx.TimeoutException as e:
        logger.error("Scheme network search timed out: %s", e)
        safe_update_current_span(
            metadata={"tool": "scheme.search", "error_type": "timeout"}
        )
        return "Scheme search request timed out. Please try again later."

    except httpx.RequestError as e:
        logger.error("Scheme network search request failed: %s", e)
        safe_update_current_span(
            metadata={"tool": "scheme.search", "error_type": "request_error"}
        )
        return f"Scheme search request failed: {e!s}"

    except Exception as e:
        logger.error("Error searching schemes: %s for query: %s", e, query)
        safe_update_current_span(
            metadata={"tool": "scheme.search", "error_type": "exception"}
        )
        raise ModelRetry("Error searching schemes, please try again") from e


if search_schemes.__doc__:
    search_schemes.__doc__ = search_schemes.__doc__.replace(
        "PLACEHOLDER_SCHEME_CODES",
        f"Available Qdrant scheme codes: {format_qdrant_scheme_codes_for_doc()}.",
    )


async def search_documents(
    query: str, 
    top_k: int = 10, 
) -> str:
    """
    Semantic search for documents. Use this tool to search for relevant documents.
    
    Args:
        query: The search query in *English* (required)
        top_k: Maximum number of results to return (default: 10)
        
    Returns:
        search_results: Formatted list of documents
    """
    try:
        # Initialize Marqo client
        endpoint_url = os.getenv('MARQO_ENDPOINT_URL')
        if not endpoint_url:
            raise ValueError("Marqo endpoint URL is required")
        
        index_name = os.getenv('MARQO_INDEX_NAME', 'sunbird-va-index')
        if not index_name:
            raise ValueError("Marqo index name is required")
        
        client = marqo.Client(url=endpoint_url)
        logger.info(f"Searching for '{query}' in index '{index_name}'")
        
        # Perform search
        search_params = {
            "q": query,
            "limit": top_k,
            "filter_string": "type:document",
            "search_method": "hybrid",
            "hybrid_parameters": {
                "retrievalMethod": "disjunction",
                "rankingMethod": "rrf",
                "alpha": 0.5,
                "rrfK": 60,
            },        
        }
        
        results = client.index(index_name).search(**search_params)['hits']
        
        if len(results) == 0:
            return f"No results found for `{query}`"
        else:            
            search_hits = [SearchHit(**hit) for hit in results]            
            # Convert back to dict format for compatibility
            document_string = '\n\n----\n\n'.join([str(document) for document in search_hits])
            return "> Search Results for `" + query + "`\n\n" + document_string
    except Exception as e:
        logger.error(f"Error searching documents: {e} for query: {query}")
        raise ModelRetry("Error searching documents, please try again")


async def search_videos(
    query: str, 
    top_k: int = 3, 
) -> str:
    """
    Semantic search for videos. Use this tool when recommending videos to the farmer.
    
    Args:
        query: The search query in *English* (required)
        top_k: Maximum number of results to return (default: 3)
        
    Returns:
        search_results: Formatted list of videos
    """
    try:
        # Initialize Marqo client
        endpoint_url = os.getenv('MARQO_ENDPOINT_URL')
        if not endpoint_url:
            raise ValueError("Marqo endpoint URL is required")
        
        index_name = os.getenv('MARQO_INDEX_NAME', 'sunbird-va-index')
        if not index_name:
            raise ValueError("Marqo index name is required")
        
        client = marqo.Client(url=endpoint_url)
        logger.info(f"Searching for '{query}' in index '{index_name}'")
        
        # Perform search using just tensor search
        search_params = {
            "q": query,
            "limit": top_k,
            "filter_string": "type:video",
            "search_method": "tensor",
        }
        
        results = client.index(index_name).search(**search_params)['hits']
        
        if len(results) == 0:
            return f"No videos found for `{query}`"
        else:            
            search_hits = [SearchHit(**hit) for hit in results]            
            video_string = '\n\n----\n\n'.join([str(document) for document in search_hits])
            return "> Videos for `" + query + "`\n\n" + video_string
        
    except Exception as e:
        logger.error(f"Error searching documents: {e} for query: {query}")
        raise ModelRetry("Error searching documents, please try again")


async def search_pests_diseases(
    query: str, 
    top_k: int = 10, 
) -> str:
    """
    Semantic search for pests and diseases information. Use this tool to search for relevant pests and diseases data.
    
    Args:
        query: The search query in *English* (required)
        top_k: Maximum number of results to return (default: 10)
        
    Returns:
        search_results: Formatted list of pests and diseases information
    """
    try:
        # Initialize Marqo client
        endpoint_url = os.getenv('MARQO_ENDPOINT_URL')
        if not endpoint_url:
            raise ValueError("Marqo endpoint URL is required")
        
        # Use separate index for pests and diseases
        index_name = os.getenv('MARQO_PESTS_DISEASES_INDEX_NAME')
        if not index_name:
            raise ValueError("Marqo pests and diseases index name is required.")
        
        client = marqo.Client(url=endpoint_url)
        logger.info(f"Searching for pests/diseases '{query}' in index '{index_name}'")
        
        # Perform search
        search_params = {
            "q": query,
            "limit": top_k,
            "search_method": "hybrid",
            "hybrid_parameters": {
                "retrievalMethod": "disjunction",
                "rankingMethod": "rrf",
                "alpha": 0.5,
                "rrfK": 60,
            },        
        }
        
        results = client.index(index_name).search(**search_params)['hits']
        
        if len(results) == 0:
            return f"No pests or diseases information found for `{query}`"
        else:            
            search_hits = [SearchHit(**hit) for hit in results]            
            # Convert back to dict format for compatibility
            document_string = '\n\n----\n\n'.join([str(document) for document in search_hits])
            return "> Pests & Diseases Search Results for `" + query + "`\n\n" + document_string
    except Exception as e:
        logger.error(f"Error searching pests and diseases: {e} for query: {query}")
        raise ModelRetry("Error searching pests and diseases, please try again")