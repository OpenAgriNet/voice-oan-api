"""Structural validation shared by glossary startup and CI checks."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


GLOSSARY_FIELDS = {
    "en",
    "gu",
    "transliteration",
    "gu_input_aliases",
    "transliteration_input_aliases",
}
POLICY_FIELDS = {"preferred", "allowed_aliases", "input_aliases", "forbidden"}


class GlossaryValidationError(ValueError):
    """Raised when glossary assets are unsafe or internally inconsistent."""


def normalize_concept(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def load_json_object(path: Path) -> Any:
    """Load JSON without hiding malformed-file errors."""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _require_nonblank_string(value: Any, location: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{location} must be a non-blank string")


def _validate_alias_list(
    value: Any,
    location: str,
    owner: str,
    owners: dict[str, set[str]],
    errors: list[str],
) -> None:
    if not isinstance(value, list):
        errors.append(f"{location} must be a list")
        return

    seen: set[str] = set()
    for alias_index, alias in enumerate(value):
        alias_location = f"{location}[{alias_index}]"
        _require_nonblank_string(alias, alias_location, errors)
        if not isinstance(alias, str) or not alias.strip():
            continue
        normalized = normalize_concept(alias)
        if normalized in seen:
            errors.append(f"{location} contains duplicate alias {alias!r}")
        seen.add(normalized)
        owners[normalized].add(owner)


def _find_replacement_cycles(replacements: dict[str, str]) -> list[list[str]]:
    normalized = {
        normalize_concept(source): normalize_concept(target)
        for source, target in replacements.items()
        if isinstance(source, str)
        and source.strip()
        and isinstance(target, str)
        and target.strip()
    }
    cycles: list[list[str]] = []
    completed: set[str] = set()
    for start in normalized:
        if start in completed:
            continue
        order: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while current in normalized and current not in completed:
            if current in positions:
                cycles.append(order[positions[current] :] + [current])
                break
            positions[current] = len(order)
            order.append(current)
            current = normalized[current]
        completed.update(order)
    return cycles


def validate_glossary_assets(glossary: Any, policy: Any) -> None:
    """Validate both terminology assets and raise one actionable error."""
    errors: list[str] = []

    if not isinstance(glossary, list):
        raise GlossaryValidationError("glossary must be a JSON list")
    if not isinstance(policy, dict):
        raise GlossaryValidationError("term policy must be a JSON object")

    unknown_policy_fields = set(policy) - POLICY_FIELDS
    missing_policy_fields = POLICY_FIELDS - set(policy)
    if unknown_policy_fields:
        errors.append(f"policy has unknown fields: {sorted(unknown_policy_fields)}")
    if missing_policy_fields:
        errors.append(f"policy is missing fields: {sorted(missing_policy_fields)}")

    concepts: dict[str, int] = {}
    gu_alias_owners: dict[str, set[str]] = defaultdict(set)
    transliteration_alias_owners: dict[str, set[str]] = defaultdict(set)

    for index, row in enumerate(glossary):
        location = f"glossary[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{location} must be an object")
            continue
        unknown_fields = set(row) - GLOSSARY_FIELDS
        missing_fields = GLOSSARY_FIELDS - set(row)
        if unknown_fields:
            errors.append(f"{location} has unknown fields: {sorted(unknown_fields)}")
        if missing_fields:
            errors.append(f"{location} is missing fields: {sorted(missing_fields)}")

        for field in ("en", "gu", "transliteration"):
            _require_nonblank_string(row.get(field), f"{location}.{field}", errors)

        english = row.get("en")
        if not isinstance(english, str) or not english.strip():
            continue
        concept = normalize_concept(english)
        if concept in concepts:
            errors.append(
                f"duplicate English concept {english!r} at rows {concepts[concept]} and {index}"
            )
        else:
            concepts[concept] = index

        _validate_alias_list(
            row.get("gu_input_aliases"),
            f"{location}.gu_input_aliases",
            concept,
            gu_alias_owners,
            errors,
        )
        _validate_alias_list(
            row.get("transliteration_input_aliases"),
            f"{location}.transliteration_input_aliases",
            concept,
            transliteration_alias_owners,
            errors,
        )

    for label, owners in (
        ("Gujarati", gu_alias_owners),
        ("transliteration", transliteration_alias_owners),
    ):
        for alias, assigned_concepts in owners.items():
            if len(assigned_concepts) > 1:
                errors.append(
                    f"{label} input alias {alias!r} belongs to multiple concepts: "
                    f"{sorted(assigned_concepts)}"
                )

    for field in POLICY_FIELDS:
        section = policy.get(field)
        if not isinstance(section, dict):
            errors.append(f"policy.{field} must be an object")
            continue
        for key, value in section.items():
            _require_nonblank_string(key, f"policy.{field} key", errors)
            if field in {"allowed_aliases", "input_aliases"}:
                if not isinstance(value, list):
                    errors.append(f"policy.{field}[{key!r}] must be a list")
                    continue
                seen: set[str] = set()
                for index, alias in enumerate(value):
                    _require_nonblank_string(alias, f"policy.{field}[{key!r}][{index}]", errors)
                    if isinstance(alias, str) and alias.strip():
                        normalized = normalize_concept(alias)
                        if normalized in seen:
                            errors.append(f"policy.{field}[{key!r}] contains duplicate {alias!r}")
                        seen.add(normalized)
            else:
                _require_nonblank_string(value, f"policy.{field}[{key!r}]", errors)

            if (
                field != "forbidden"
                and isinstance(key, str)
                and key.strip()
                and normalize_concept(key) not in concepts
            ):
                errors.append(f"policy.{field} key {key!r} has no glossary concept")

    preferred = policy.get("preferred", {})
    forbidden = policy.get("forbidden", {})
    if isinstance(preferred, dict):
        for english, gujarati in preferred.items():
            concept = normalize_concept(english) if isinstance(english, str) else ""
            if concept in concepts and isinstance(gujarati, str) and gujarati.strip():
                glossary_value = glossary[concepts[concept]].get("gu")
                if glossary_value != gujarati:
                    errors.append(
                        f"policy.preferred[{english!r}] is {gujarati!r}, but the glossary "
                        f"uses {glossary_value!r}"
                    )
            if (
                isinstance(gujarati, str)
                and isinstance(forbidden, dict)
                and normalize_concept(gujarati)
                in {normalize_concept(key) for key in forbidden if isinstance(key, str)}
            ):
                errors.append(f"preferred Gujarati value {gujarati!r} is also forbidden")

    if isinstance(forbidden, dict):
        forbidden_sources = {
            normalize_concept(key)
            for key in forbidden
            if isinstance(key, str) and key.strip()
        }
        allowed_aliases = policy.get("allowed_aliases", {})
        if isinstance(allowed_aliases, dict):
            for english, aliases in allowed_aliases.items():
                if not isinstance(aliases, list):
                    continue
                for alias in aliases:
                    if isinstance(alias, str) and normalize_concept(alias) in forbidden_sources:
                        errors.append(
                            f"policy.allowed_aliases[{english!r}] contains forbidden output {alias!r}"
                        )

        for cycle in _find_replacement_cycles(forbidden):
            errors.append(f"forbidden replacement cycle: {' -> '.join(cycle)}")

    if errors:
        raise GlossaryValidationError("Invalid glossary assets:\n- " + "\n- ".join(errors))


def validate_files(glossary_path: Path, policy_path: Path) -> None:
    validate_glossary_assets(load_json_object(glossary_path), load_json_object(policy_path))


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    validate_files(root / "assets/glossary_terms.json", root / "assets/gu_term_policy.json")
    print("Glossary validation passed")
