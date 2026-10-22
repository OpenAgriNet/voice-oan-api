"""Markdown skill packages: one source for tool bindings and localized sections."""
from functools import lru_cache
from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader, pass_context
import yaml

PROMPT_DIR = Path(__file__).resolve().parents[1] / "assets/prompts"
AGENT = "voice"
SECTION_CALL = re.compile(r"\{\{ skill_section\('([^']+)'\) \}\}")


@lru_cache(maxsize=8)
def skill_catalog(prompt_dir: Path = PROMPT_DIR) -> dict:
    """Read trusted repository assets, never provider-supplied Markdown."""
    packages = {}
    base = Path(prompt_dir) / "capabilities" / AGENT
    for path in sorted(base.glob("*/SKILL.md")):
        parts = path.read_text().split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            raise ValueError(f"Missing YAML frontmatter: {path}")
        data = yaml.safe_load(parts[1])
        if not isinstance(data, dict) or data.get("name") != path.parent.name:
            raise ValueError(f"Skill name must match its directory: {path}")
        tools = data.get("tools", [])
        if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools) or len(tools) != len(set(tools)):
            raise ValueError(f"Invalid tool bindings: {path}")
        if not data.get("description") or not isinstance(data.get("sections", {}), dict):
            raise ValueError(f"Missing description or invalid sections: {path}")
        packages[data["name"]] = data
    if not packages:
        raise ValueError(f"No skill packages in {base}")
    return packages


def tool_groups(prompt_dir: Path = PROMPT_DIR) -> dict[str, frozenset[str]]:
    return {name: frozenset(package["tools"]) for name, package in skill_catalog(prompt_dir).items()
            if package["tools"]}


def section_spec(key: str, language: str, prompt_dir: Path = PROMPT_DIR) -> dict:
    group, section = key.split("/", 1)
    packages = skill_catalog(prompt_dir)
    spec = packages[group]["sections"][section]
    if "languages" in spec and language not in spec["languages"]:
        raise ValueError(f"Section {key} is not defined for {language}")
    groups = tool_groups(prompt_dir)
    tools = set(spec.get("tools", ()))
    for name in spec.get("when_any", [group] if group in groups else []):
        tools.update(groups[name])
    if not tools or not tools <= set().union(*groups.values()):
        raise ValueError(f"Unknown or missing tool bindings: {key}")
    relative = Path("capabilities") / AGENT / group / spec["file"].format(language=language)
    resolved = (Path(prompt_dir) / relative).resolve()
    if not resolved.is_relative_to((Path(prompt_dir) / "capabilities" / AGENT).resolve()):
        raise ValueError(f"Section escapes its prompt directory: {key}")
    if not resolved.is_file():
        raise ValueError(f"Missing {language} section: {relative}")
    return {"id": key, "path": relative.as_posix(), "tools": sorted(tools)}


def section_index(prompt_dir: Path = PROMPT_DIR) -> dict[str, list[dict]]:
    """Derived index; root templates specify layout, packages specify content/bindings."""
    result = {}
    for root in sorted(Path(prompt_dir).glob(f"{AGENT}_*.md")):
        language = root.stem.rsplit("_", 1)[1]
        keys = SECTION_CALL.findall(root.read_text())
        if len(keys) != len(set(keys)):
            raise ValueError(f"Duplicate skill section in {root}")
        result[root.stem] = [section_spec(key, language, prompt_dir) for key in keys]
    return result


def prompt_environment(prompt_dir: Path = PROMPT_DIR) -> Environment:
    prompt_dir = Path(prompt_dir)
    env = Environment(loader=FileSystemLoader(prompt_dir), autoescape=False)

    @pass_context
    def skill_section(context, key: str) -> str:
        language = Path(context.name).stem.rsplit("_", 1)[1]
        spec = section_spec(key, language, prompt_dir)
        enabled = context.get("capability_enabled")
        if enabled is not None and not enabled(spec["tools"]):
            return ""
        return env.get_template(spec["path"]).render(**context.get_all())

    env.globals["skill_section"] = skill_section
    return env
