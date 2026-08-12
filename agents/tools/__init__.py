"""
Tools for the Sunbird VA API.
"""
from __future__ import annotations

from agents.tools.search import search_documents, search_videos
from agents.tools.weather import weather_forecast, weather_historical
from agents.tools.mandi import mandi_prices
from agents.tools.warehouse import warehouse_data
from agents.tools.maps import reverse_geocode, forward_geocode  
from pydantic_ai import Tool
from agents.tools.terms import search_terms
from agents.tools.scheme_info import get_scheme_codes, get_scheme_info
from agents.tools.agri_services import agri_services
from agents.tools.staff_contact import contact_agricultural_staff

try:
    from langfuse import observe  # type: ignore
except Exception:  # pragma: no cover
    observe = None  # type: ignore

from app.langfuse_client import get_langfuse


def _trace_tool(fn, *, name: str):
    """
    Wrap a tool function so Langfuse captures tool input/output/errors.

    We only wrap when Langfuse is configured; otherwise we return `fn` unchanged.
    """
    if observe is None or get_langfuse() is None:
        return fn
    return observe(name=name, as_type="tool")(fn)


TOOLS = [
    Tool(
        _trace_tool(search_terms, name="search_terms"),
        takes_ctx=False,
    ),
    Tool(
        _trace_tool(search_documents, name="search_documents"),
        takes_ctx=True,
    ),
    Tool(
        _trace_tool(search_videos, name="search_videos"),
        takes_ctx=True,
    ),
    Tool(
        _trace_tool(weather_forecast, name="weather_forecast"),
        takes_ctx=True,
    ),
    Tool(
        _trace_tool(weather_historical, name="weather_historical"),
        takes_ctx=True,
    ),
    Tool(
        _trace_tool(mandi_prices, name="mandi_prices"),
        takes_ctx=True,
    ),
    Tool(
        _trace_tool(warehouse_data, name="warehouse_data"),
        takes_ctx=True,
    ),
    Tool(
        _trace_tool(forward_geocode, name="forward_geocode"),
        takes_ctx=False,
    ),
    Tool(
        _trace_tool(get_scheme_codes, name="get_scheme_codes"),
        takes_ctx=False,
    ),
    Tool(
        _trace_tool(get_scheme_info, name="get_scheme_info"),
        takes_ctx=False,
    ),
    Tool(
        _trace_tool(agri_services, name="agri_services"),
        takes_ctx=False,
        docstring_format='auto', 
        require_parameter_descriptions=True,
    ),
    Tool(
        _trace_tool(contact_agricultural_staff, name="contact_agricultural_staff"),
        takes_ctx=False,
        docstring_format='auto', 
        require_parameter_descriptions=True,
    ),

]
