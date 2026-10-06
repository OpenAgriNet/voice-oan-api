"""Dependency-free wiring checks for the Voice tool parity addendum.

Run with: python3 tests/test_voice_tool_parity.py
"""

import ast
import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_MODULES = {
    "agents.tools.maha_vistaar": {"call_maha_vistaar_network"},
    "agents.tools.amul_vistaar": {"call_amul_vistaar_network"},
    "agents.tools.smam_scheme_status": {"check_smam_scheme_status"},
    "agents.tools.aif": {
        "initiate_aif_otp",
        "verify_aif_otp",
        "check_aif_loan_status",
        "check_aif_grievance_status",
    },
    "agents.tools.gfr": {"gfr_get_crop_registries", "gfr_get_recommendations"},
    "agents.tools.sathi_seed": {
        "get_sathi_crop_groups",
        "list_sathi_crops_in_group",
        "search_sathi_seed_availability",
    },
    "agents.tools.search": {"search_schemes"},
}


def check_registered_tools() -> None:
    registry_path = REPO_ROOT / "agents" / "tools" / "__init__.py"
    registry = ast.parse(registry_path.read_text(encoding="utf-8"))
    imported = {}
    registered = set()

    for node in ast.walk(registry):
        if isinstance(node, ast.ImportFrom):
            imported[node.module] = {alias.asname or alias.name for alias in node.names}
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Tool"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            registered.add(node.args[0].id)

    for module, names in TOOL_MODULES.items():
        assert names <= imported.get(module, set()), f"Missing imports from {module}"
        assert names <= registered, f"Unregistered tools from {module}"
    assert "get_scheme_info" not in registered, "Legacy scheme tool is still registered"


def check_prompt_wiring() -> None:
    voice_agent = (REPO_ROOT / "agents" / "voice.py").read_text(encoding="utf-8")
    assert 'prompt_file = f"voice_{language_code}"' in voice_agent
    assert "get_vector_schemes_prompt_block" in voice_agent
    prompts = list((REPO_ROOT / "assets" / "prompts").glob("voice_*.md"))
    assert len(prompts) == 11, f"Expected 11 localized prompts, found {len(prompts)}"
    for prompt_path in prompts:
        prompt = prompt_path.read_text(encoding="utf-8")
        assert "get_scheme_info" not in prompt, f"{prompt_path.name} retains legacy routing"
        for variable in ("vector_scheme_count", "vector_schemes_bullets", "vector_schemes_identifiers"):
            assert variable in prompt, f"{prompt_path.name} is missing {variable}"
        for names in TOOL_MODULES.values():
            for name in names:
                assert name in prompt, f"{prompt_path.name} does not describe {name}"
        if prompt_path.stem != "voice_en":
            assert "## PMFBY GRIEVANCE WORKFLOW" not in prompt, (
                f"{prompt_path.name} still has an English PMFBY workflow heading"
            )
        for call in (
            "initiate_pmfby_grievance_otp(phone_number)",
            "check_pmfby_grievance_otp(otp, phone_number)",
            "pmfby_submit_grievance(otp, phone_number, request_year, request_season, application_no, grievance_description)",
            "pmfby_grievance_status(phone_number, grievance_support_ticket_no)",
        ):
            assert call in prompt, f"{prompt_path.name} changed PMFBY arguments: {call}"


def check_redis_scheme_catalog() -> None:
    module_path = REPO_ROOT / "helpers" / "master_catalog.py"
    spec = importlib.util.spec_from_file_location("voice_master_catalog_test", module_path)
    catalog = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(catalog)

    snapshot = {
        "version": 4,
        "entries": [
            {"code": "pmfby", "name": "PMFBY", "aliases": ["crop insurance"], "tool_name": "search_schemes"},
            {"code": "aif", "name": "Agriculture Infrastructure Fund", "aliases": [], "tool_name": "search_schemes"},
            {"code": "old", "name": "Old route", "aliases": [], "tool_name": "get_scheme_info"},
        ],
        "prompt": {
            "vector_scheme_count": 2,
            "vector_schemes_bullets": "PMFBY and AIF",
            "vector_schemes_identifiers": "pmfby / crop insurance; aif",
        },
    }

    class FakeRedis:
        requested_key = None

        def get(self, key):
            self.requested_key = key
            return json.dumps(snapshot)

    fake_redis = FakeRedis()
    catalog._get_redis_client = lambda: fake_redis
    catalog._redis_key_for_tier = lambda tier: f"master-catalog:{tier}:snapshot"
    loaded = catalog.get_master_catalog_snapshot("dev")
    assert fake_redis.requested_key == "master-catalog:dev:snapshot"
    assert loaded == snapshot
    assert [item["code"] for item in catalog.get_vector_scheme_entries(loaded)] == ["pmfby", "aif"]

    catalog.get_master_catalog_snapshot = lambda tier=None: snapshot
    assert catalog.get_vector_schemes_prompt_block() == snapshot["prompt"]


if __name__ == "__main__":
    check_registered_tools()
    check_prompt_wiring()
    check_redis_scheme_catalog()
    print("Voice tool parity wiring checks passed.")