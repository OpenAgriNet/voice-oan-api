"""
Self-check for pmfby_grievance.py pure helpers (no network calls).

Usage:
    python tests/test_pmfby_grievance.py
"""
import importlib.util
import inspect
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from pydantic_ai import ModelRetry

# Load the module directly by file path (bypasses agents/tools/__init__.py,
# which eagerly imports every tool and its heavy dependencies).
_spec = importlib.util.spec_from_file_location(
    "pmfby_grievance_standalone",
    os.path.join(REPO_ROOT, "agents", "tools", "pmfby_grievance.py"),
)
_pmfby_grievance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pmfby_grievance)

normalize_phone_for_api = _pmfby_grievance.normalize_phone_for_api
_validate_otp = _pmfby_grievance._validate_otp
_normalize_request_season_for_pmfby_api = _pmfby_grievance._normalize_request_season_for_pmfby_api
_payload_get_otp_grievance_flow = _pmfby_grievance._payload_get_otp_grievance_flow
generate_transaction_id = _pmfby_grievance.generate_transaction_id

# normalize_phone_for_api
assert normalize_phone_for_api("+91 98765 43210") == "9876543210"
assert normalize_phone_for_api("09876543210") == "9876543210"
assert normalize_phone_for_api("9876543210") == "9876543210"

# _validate_otp
assert _validate_otp("123456") == "123456"
assert _validate_otp("12 34 56") == "123456"
try:
    _validate_otp("1234")
    raise AssertionError("expected ModelRetry for 4-digit OTP")
except ModelRetry:
    pass

# _normalize_request_season_for_pmfby_api
assert _normalize_request_season_for_pmfby_api("Kharif") == "1"
assert _normalize_request_season_for_pmfby_api("rabi") == "2"
assert _normalize_request_season_for_pmfby_api("Summer") == "3"
assert _normalize_request_season_for_pmfby_api("2") == "2"

# payload builder shape (no live BAP_ENDPOINT call)
tx = generate_transaction_id("session-1", "9876543210")
payload = _payload_get_otp_grievance_flow(transaction_id=tx, phone="9876543210")
assert payload["context"]["action"] == "init"
assert payload["message"]["order"]["provider"]["id"] == "pmfby-agri"

# Regression guard: grievance submission must require an `otp` parameter
# (it must never be callable without OTP verification again).
submit_params = list(inspect.signature(_pmfby_grievance.pmfby_submit_grievance).parameters)
assert submit_params[1] == "otp", "pmfby_submit_grievance must require OTP as its first argument after ctx"

print("All pmfby_grievance self-checks passed.")
