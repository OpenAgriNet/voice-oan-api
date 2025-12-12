"""
Tools for the Sunbird VA API.
"""
from agents.tools.search import search_documents
from agents.tools.weather import weather_forecast, weather_historical
from agents.tools.mandi import mandi_prices
from agents.tools.warehouse import warehouse_data
from agents.tools.maps import reverse_geocode, forward_geocode  
from pydantic_ai import Tool
from agents.tools.terms import search_terms
from agents.tools.scheme_info import get_scheme_codes, get_scheme_info
from agents.tools.agri_services import agri_services
from agents.tools.staff_contact import contact_agricultural_staff

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
        weather_forecast,
        takes_ctx=True,
    ),
    Tool(
        weather_historical,
        takes_ctx=True,
    ),
    Tool(
        mandi_prices,
        takes_ctx=True,
    ),
    Tool(
        warehouse_data,
        takes_ctx=True,
    ),
    Tool(
        forward_geocode,
        takes_ctx=False,
    ),
    Tool(
        get_scheme_codes,
        takes_ctx=False,
    ),
    Tool(
        get_scheme_info,
        takes_ctx=True,
    ),
     Tool(
        agri_services,
        takes_ctx=False,
        docstring_format='auto', 
        require_parameter_descriptions=True,
    ),
    Tool(
        contact_agricultural_staff,
        takes_ctx=False,
        docstring_format='auto', 
        require_parameter_descriptions=True,
    ),

]
