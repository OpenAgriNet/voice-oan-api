"""Related tools load together, intersected with the session's tool allowlist."""
CAPABILITIES = {
    "schemes": {"get_scheme_info", "search_schemes"},
    "pmkisan": {"initiate_pm_kisan_status_check", "check_pm_kisan_status_with_otp",
                "pmkisan_grievance_send_otp", "pmkisan_submit_grievance", "pmkisan_grievance_status"},
    "pmfby": {"initiate_pmfby_status_check", "check_pmfby_status_with_otp",
              "initiate_pmfby_grievance_otp", "check_pmfby_grievance_otp",
              "pmfby_grievance_status", "pmfby_submit_grievance"},
    "soil_health": {"check_shc_status"},
    "crop_advice": {"search_terms", "search_documents", "search_videos", "search_pests_diseases"},
    "weather": {"weather_forecast"},
    "location": {"forward_geocode", "reverse_geocode"},
    "mandi": {"search_commodity", "get_mandi_prices"},
    "feedback": {"submit_feedback"},
}
