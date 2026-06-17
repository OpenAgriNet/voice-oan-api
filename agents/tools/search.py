"""
Vector search via direct HTTP to the Marqo/search backend.
"""
import os
import re
import httpx
from typing import Literal
from pydantic import BaseModel, Field
from pydantic_ai import ModelRetry
from helpers.utils import get_logger
from agents.tools.terms import normalize_text_with_glossary

logger = get_logger(__name__)

DocumentType = Literal['video', 'document']

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
        endpoint_url = os.getenv('MARQO_ENDPOINT_URL')
        if not endpoint_url:
            raise ValueError("Marqo endpoint URL is required")

        index_name = os.getenv('MARQO_INDEX_NAME', 'sunbird-va-index')
        if not index_name:
            raise ValueError("Marqo index name is required")

        search_url = endpoint_url.rstrip("/") + f"/indexes/{index_name}/search"
        logger.info(f"Searching for '{query}' in index '{index_name}'")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                search_url,
                json={"q": query, "limit": top_k, "filter": "type = document"},
                timeout=httpx.Timeout(20.0, read=30.0),
            )
        response.raise_for_status()
        results = response.json().get("hits", [])

        if not results:
            return f"No results found for `{query}`"
        search_hits = [SearchHit(**hit) for hit in results]
        document_string = '\n\n----\n\n'.join([str(doc) for doc in search_hits])
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
        endpoint_url = os.getenv('MARQO_ENDPOINT_URL')
        if not endpoint_url:
            raise ValueError("Marqo endpoint URL is required")

        index_name = os.getenv('MARQO_INDEX_NAME', 'sunbird-va-index')
        if not index_name:
            raise ValueError("Marqo index name is required")

        search_url = endpoint_url.rstrip("/") + f"/indexes/{index_name}/search"
        logger.info(f"Searching for '{query}' in index '{index_name}'")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                search_url,
                json={"q": query, "limit": top_k, "filter": "type = video"},
                timeout=httpx.Timeout(20.0, read=30.0),
            )
        response.raise_for_status()
        results = response.json().get("hits", [])

        if not results:
            return f"No videos found for `{query}`"
        search_hits = [SearchHit(**hit) for hit in results]
        video_string = '\n\n----\n\n'.join([str(doc) for doc in search_hits])
        return "> Videos for `" + query + "`\n\n" + video_string

    except Exception as e:
        logger.error(f"Error searching videos: {e} for query: {query}")
        raise ModelRetry("Error searching videos, please try again")


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
        endpoint_url = os.getenv('MARQO_ENDPOINT_URL')
        if not endpoint_url:
            raise ValueError("Marqo endpoint URL is required")

        index_name = os.getenv('MARQO_PESTS_DISEASES_INDEX_NAME')
        if not index_name:
            raise ValueError("Marqo pests and diseases index name is required.")

        search_url = endpoint_url.rstrip("/") + f"/indexes/{index_name}/search"
        logger.info(f"Searching for pests/diseases '{query}' in index '{index_name}'")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                search_url,
                json={"q": query, "limit": top_k},
                timeout=httpx.Timeout(20.0, read=30.0),
            )
        response.raise_for_status()
        results = response.json().get("hits", [])

        if not results:
            return f"No pests or diseases information found for `{query}`"
        search_hits = [SearchHit(**hit) for hit in results]
        document_string = '\n\n----\n\n'.join([str(doc) for doc in search_hits])
        return "> Pests & Diseases Search Results for `" + query + "`\n\n" + document_string
    except Exception as e:
        logger.error(f"Error searching pests and diseases: {e} for query: {query}")
        raise ModelRetry("Error searching pests and diseases, please try again")