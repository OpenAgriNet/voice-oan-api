"""
Tools for the BharatVistaar AI Agent.
"""
# from agents.tools.search_beckn import search_documents
from pydantic_ai import Tool
from agents.tools.maha_vistaar import call_maha_vistaar_network
from agents.tools.amul_vistaar import call_amul_vistaar_network
from agents.tools.smam_scheme_status import check_smam_scheme_status
from agents.tools.aif import (
    initiate_aif_otp,
    verify_aif_otp,
    check_aif_loan_status,
    check_aif_grievance_status,
)
from agents.tools.gfr import gfr_get_crop_registries, gfr_get_recommendations
from agents.tools.sathi_seed import (
    get_sathi_crop_groups,
    list_sathi_crops_in_group,
    search_sathi_seed_availability,
)
from agents.tools.pmkisan_scheme_status import initiate_pm_kisan_status_check, check_pm_kisan_status_with_otp
from agents.tools.pmfby_scheme_status import initiate_pmfby_status_check, check_pmfby_status_with_otp
from agents.tools.shc_scheme_status import check_shc_status
from agents.tools.pmkisan_grievance import (
    pmkisan_grievance_send_otp,
    pmkisan_submit_grievance,
    pmkisan_grievance_status,
)
from agents.tools.pmfby_grievance import (
    initiate_pmfby_grievance_otp,
    check_pmfby_grievance_otp,
    pmfby_grievance_status,
    pmfby_submit_grievance,
)
from agents.tools.terms import search_terms
from agents.tools.search import search_documents, search_videos, search_pests_diseases, search_schemes
from agents.tools.weather import weather_forecast
from agents.tools.maps import reverse_geocode, forward_geocode
from agents.tools.mandi import get_mandi_prices
from agents.tools.commodity import search_commodity
from agents.tools.feedback import submit_feedback


TOOLS = [
    Tool(call_maha_vistaar_network, takes_ctx=True, strict=False),
    Tool(call_amul_vistaar_network, takes_ctx=True, strict=False),
    Tool(check_smam_scheme_status, takes_ctx=True, strict=False),
    Tool(initiate_aif_otp, takes_ctx=True, strict=False),
    Tool(verify_aif_otp, takes_ctx=True, strict=False),
    Tool(check_aif_loan_status, takes_ctx=True, strict=False),
    Tool(check_aif_grievance_status, takes_ctx=True, strict=False),
    Tool(gfr_get_crop_registries, takes_ctx=True, strict=False),
    Tool(gfr_get_recommendations, takes_ctx=True, strict=False),
    Tool(get_sathi_crop_groups, takes_ctx=False, strict=False),
    Tool(list_sathi_crops_in_group, takes_ctx=False, strict=False),
    Tool(search_sathi_seed_availability, takes_ctx=True, strict=False),
    Tool(
        initiate_pm_kisan_status_check,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        check_pm_kisan_status_with_otp,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        initiate_pmfby_status_check,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        check_pmfby_status_with_otp,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        check_shc_status,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        pmkisan_grievance_send_otp,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        pmkisan_submit_grievance,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        pmkisan_grievance_status,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        initiate_pmfby_grievance_otp,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        check_pmfby_grievance_otp,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        pmfby_grievance_status,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        pmfby_submit_grievance,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        search_terms,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        search_documents,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        search_schemes,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        search_videos,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        search_pests_diseases,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        weather_forecast,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        forward_geocode,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        reverse_geocode,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        get_mandi_prices,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        search_commodity,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        submit_feedback,
        takes_ctx=True,
        strict=False,
    ),
]
