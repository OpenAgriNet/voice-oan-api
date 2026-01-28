"""
Tools for the BharatVistaar AI Agent.
"""
#from agents.tools.common import reasoning_tool, planning_tool
# from agents.tools.search_beckn import search_documents
# from agents.tools.maps import reverse_geocode, forward_geocode  
from pydantic_ai import Tool
from agents.tools.scheme_info import get_scheme_info
from agents.tools.pmkisan_scheme_status import initiate_pm_kisan_status_check, check_pm_kisan_status_with_otp
from agents.tools.pmfby_scheme_status import check_pmfby_status
from agents.tools.shc_scheme_status import check_shc_status
from agents.tools.grievance import submit_grievance, grievance_status
from agents.tools.terms import search_terms
from agents.tools.search import search_documents
from agents.tools.search import search_videos
from agents.tools.search import search_pests_diseases
from agents.tools.weather import weather_forecast
from agents.tools.maps import reverse_geocode, forward_geocode


TOOLS = [
    Tool(
        get_scheme_info,
        takes_ctx=False,
    ),
    Tool(
        initiate_pm_kisan_status_check,
        takes_ctx=True,
    ),
    Tool(
        check_pm_kisan_status_with_otp,
        takes_ctx=True,
    ),
    Tool(
        check_pmfby_status,
        takes_ctx=False,
    ),
    Tool(
        check_shc_status,
        takes_ctx=False,
    ),
    Tool(
        submit_grievance,
        takes_ctx=False,
    ),
    Tool(
        grievance_status,
        takes_ctx=False,
    ),
    Tool(
        search_terms,
        takes_ctx=False,
    ),
    Tool(
        search_documents,
        takes_ctx=False,
    ),
    Tool(
        search_videos,
        takes_ctx=False,
    ),
    Tool(
        search_pests_diseases,
        takes_ctx=False,
    ),
    Tool(
        weather_forecast,
        takes_ctx=False,
    ),
    Tool(
        forward_geocode,
        takes_ctx=False,
    ),
    Tool(
        reverse_geocode,
        takes_ctx=False,
    ),
]

