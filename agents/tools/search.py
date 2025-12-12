"""
Marqo client implementation for vector search.
"""
import os
import re
import marqo
from typing import Optional, Literal
from pydantic import BaseModel, Field
from pydantic_ai import ModelRetry, RunContext
from helpers.utils import get_logger
from agents.deps import FarmerContext
from agents.tools.common import get_nudge_message, send_nudge_message_raya


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
        return cleaned

    def __str__(self) -> str:
        if self.type == 'document':
            return f"**{self.name}**\n" + "```\n" + self.processed_text +  "\n```\n" 
        else:
            return f"**[{self.name}]({self.source})**\n" + "```\n" + self.processed_text + "\n```\n"


# NOTE: CTX is now available for this tool so we can send nudge messages if needed.

async def search_documents(
    ctx: RunContext[FarmerContext],
    query: str, 
    top_k: int = 10, 
    type: Optional[str] = None
) -> str:
    """
    Semantic search for videos and documents. Use this tool to search for relevant videos and documents.
    
    Args:
        ctx: The context containing session information
        query: The search query in *English* (required)
        top_k: Maximum number of results to return (default: 10)
        type: Filter by document type: [`video`, `document`]. Default is None, which means all types are considered.
        
    Returns:
        search_results: Formatted string with search results
    """
    try:
        # Send nudge message asynchronously without blocking
        nudge_message = get_nudge_message("search_documents", ctx.deps.lang_code)
        result = await send_nudge_message_raya(nudge_message, ctx.deps.session_id, ctx.deps.process_id)
        logger.info(f"Nudge message sent: {result}")

        # Initialize Marqo client
        endpoint_url = os.getenv('MARQO_ENDPOINT_URL')
        if not endpoint_url:
            raise ValueError("Marqo endpoint URL is required")
        
        index_name = os.getenv('MARQO_INDEX_NAME', 'sunbird-va-index')
        if not index_name:
            raise ValueError("Marqo index name is required")
        
        client = marqo.Client(url=endpoint_url)
        logger.info(f"Searching for '{query}' in index '{index_name}'")
        
        # Default to all types if none specified
        if type is None:
            filter_string = f"type:video OR type:document"
        else:
            filter_string = f"type:{type}"

        filter_string = f"({filter_string})"
            
        # Perform search
        search_params = {
            "q": query,
            "limit": top_k,
            "filter_string": filter_string,
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
        raise ModelRetry(f"Error searching documents, please try again")