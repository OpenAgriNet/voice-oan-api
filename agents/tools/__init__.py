"""
Tools for the Sunbird VA API.
"""
from pydantic_ai import Tool
from agents.tools.terms import search_terms
from agents.tools.search import search_documents
from agents.tools.farmer import get_farmer_by_mobile

TOOLS = [
    Tool(
        search_terms,
        takes_ctx=False,
    ),
    Tool(
        search_documents,
        takes_ctx=True,
    ),
    Tool(
        get_farmer_by_mobile,
        takes_ctx=False,
    )
]
