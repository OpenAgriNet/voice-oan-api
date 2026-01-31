"""
Tools for the Sunbird VA API.
"""
from pydantic_ai import Tool
from agents.tools.terms import search_terms
from agents.tools.search import search_documents
from agents.tools.farmer import get_farmer_by_mobile
from agents.tools.animal import get_animal_by_tag
from agents.tools.cvcc import get_cvcc_health_details

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
        docstring_format='auto',
        require_parameter_descriptions=True,
    ),
    Tool(
        get_animal_by_tag,
        takes_ctx=False,
        docstring_format='auto',
        require_parameter_descriptions=True,
    ),
    Tool(
        get_cvcc_health_details,
        takes_ctx=False,
        docstring_format='auto',
        require_parameter_descriptions=True,
    ),
]
