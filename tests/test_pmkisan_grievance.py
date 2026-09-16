"""
Self-check for pmkisan_grievance.py: OTP-first flow is required (no more
direct LodgeGrievance submission without OTP verification).

Usage:
    python tests/test_pmkisan_grievance.py
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
    "pmkisan_grievance_standalone",
    os.path.join(REPO_ROOT, "agents", "tools", "pmkisan_grievance.py"),
)
_pmkisan_grievance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pmkisan_grievance)

GRIEVANCE_MAPPING = _pmkisan_grievance.GRIEVANCE_MAPPING
_validate_submit_grievance_inputs = _pmkisan_grievance._validate_submit_grievance_inputs
generate_transaction_id = _pmkisan_grievance.generate_transaction_id

# grievance type mapping loads from assets/grievance_types.json
assert GRIEVANCE_MAPPING, "GRIEVANCE_MAPPING should not be empty"
some_type = next(iter(GRIEVANCE_MAPPING))

# OTP is mandatory before submitting a grievance
try:
    _validate_submit_grievance_inputs("REG123", some_type, "Payment not received in my account", otp=None)
    raise AssertionError("expected ModelRetry when otp is missing")
except ModelRetry:
    pass

try:
    _validate_submit_grievance_inputs("REG123", some_type, "Payment not received in my account", otp="")
    raise AssertionError("expected ModelRetry when otp is empty")
except ModelRetry:
    pass

# Once an OTP value is supplied, validation passes through the identifier
identifier_value, reg_no_clean = _validate_submit_grievance_inputs(
    "REG123", some_type, "Payment not received in my account", otp="1234"
)
assert identifier_value == "REG123"
assert reg_no_clean == "REG123"

# Exported tools require an `otp` parameter (regression guard: submission/status
# must never be callable without OTP verification again).
submit_params = list(inspect.signature(_pmkisan_grievance.pmkisan_submit_grievance).parameters)
assert "otp" in submit_params

status_params = list(inspect.signature(_pmkisan_grievance.pmkisan_grievance_status).parameters)
assert "otp" in status_params

# transaction id is deterministic for the same session/identifier pair
tx1 = generate_transaction_id("session-1", "REG123")
tx2 = generate_transaction_id("session-1", "REG123")
assert tx1 == tx2

print("All pmkisan_grievance self-checks passed.")
