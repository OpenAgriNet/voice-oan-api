"""Dependency-free wiring checks for the Voice tool parity addendum.

Run with: python3 tests/test_voice_tool_parity.py
"""

import ast
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


def check_prompt_wiring() -> None:
    voice_agent = (REPO_ROOT / "agents" / "voice.py").read_text(encoding="utf-8")
    assert 'prompt_file = f"voice_{language_code}"' in voice_agent
    prompts = list((REPO_ROOT / "assets" / "prompts").glob("voice_*.md"))
    assert len(prompts) == 11, f"Expected 11 localized prompts, found {len(prompts)}"
    for prompt_path in prompts:
        prompt = prompt_path.read_text(encoding="utf-8")
        for names in TOOL_MODULES.values():
            for name in names:
                assert name in prompt, f"{prompt_path.name} does not describe {name}"


if __name__ == "__main__":
    check_registered_tools()
    check_prompt_wiring()
    print("Voice tool parity wiring checks passed.")