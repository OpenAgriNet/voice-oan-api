"""amul-oan-api must be able to read what this voice contract sends.

Reads telemetry/eras.yaml and telemetry/mappings/voice.yaml from an amul-oan-api
checkout: AMUL_OAN_API_PATH, or a sibling folder. CI checks out its main branch,
so a change there has to land before the voice change that needs it.
"""

import json
import os
from pathlib import Path

import pytest
import yaml

from app.services.telemetry_stamps import VOICE_TELEMETRY_SCHEMA_VERSION

REPO = Path(__file__).resolve().parents[1]
CONTRACT = json.loads(
    (REPO / "telemetry" / "contracts" / f"{VOICE_TELEMETRY_SCHEMA_VERSION}.json").read_text(encoding="utf-8")
)
AMUL = Path(os.getenv("AMUL_OAN_API_PATH") or REPO.parent / "amul-oan-api")
ERAS = AMUL / "telemetry" / "eras.yaml"
MAPPINGS = AMUL / "telemetry" / "mappings" / "voice.yaml"

# Trace fields a mapping path can name, and the contract field each one is.
TRACE_FIELDS = {
    "sessionId": "session_id",
    "session_id": "session_id",
    "userId": "user_id",
    "user_id": "user_id",
    "input": "input",
    "output": "output",
}

pytestmark = pytest.mark.skipif(
    not (ERAS.exists() and MAPPINGS.exists()),
    reason="needs telemetry/eras.yaml and telemetry/mappings/voice.yaml from amul-oan-api",
)


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _mapping(version, payload):
    """The mapping for a version, with whatever it extends filled in."""
    contract = payload[version]
    parent = _mapping(contract["extends"], payload) if contract.get("extends") else {"root": None, "fields": {}}
    fields = dict(parent["fields"])
    for field, paths in (contract.get("fields") or {}).items():
        fields[field] = [paths] if isinstance(paths, str) else paths
    return {"root": contract.get("root") or parent["root"], "fields": fields}


def _sent(path):
    """Whether this contract sends what a mapping path reads."""
    if path in TRACE_FIELDS:
        return TRACE_FIELDS[path] in CONTRACT["trace_fields"]
    head, _, rest = path.partition(".")
    if head != "metadata" or not rest:
        return False
    if rest in CONTRACT["metadata_keys"]:
        return True
    key, _, nested = rest.partition(".")
    if key not in CONTRACT["metadata_keys"]:
        return False
    return not nested or nested in CONTRACT["nested_keys"].get(key, [])


def test_every_outcome_has_a_bucket():
    vocabulary = _load(ERAS)["voice_outcome_vocabulary"]
    bucketed = {outcome for outcomes in vocabulary.values() if isinstance(outcomes, list) for outcome in outcomes}
    missing = set(CONTRACT["outcomes"]) - bucketed

    assert not missing, (
        f"amul-oan-api has no bucket for the outcomes {sorted(missing)}. Add them to voice_outcome_vocabulary "
        "in its telemetry/eras.yaml first, or the adapters count them as unclassified."
    )


def test_amul_oan_api_reads_this_schema_version():
    payload = _load(MAPPINGS)

    assert VOICE_TELEMETRY_SCHEMA_VERSION in payload, (
        f"telemetry/mappings/voice.yaml in amul-oan-api has no {VOICE_TELEMETRY_SCHEMA_VERSION}. "
        "Add it there first, or the adapters reject these traces."
    )
    assert _mapping(VOICE_TELEMETRY_SCHEMA_VERSION, payload)["root"] == CONTRACT["root"]


def test_every_mapped_field_is_still_sent():
    mapping = _mapping(VOICE_TELEMETRY_SCHEMA_VERSION, _load(MAPPINGS))
    unreadable = {field: paths for field, paths in mapping["fields"].items() if not any(_sent(path) for path in paths)}

    assert not unreadable, (
        f"amul-oan-api reads {unreadable} from {VOICE_TELEMETRY_SCHEMA_VERSION} traces, but none of those paths "
        "are sent any more. Fix the mapping there, or bump the version."
    )


def test_a_field_is_readable_through_any_of_its_paths():
    assert _sent("metadata.agent.signed_in")
    assert _sent("metadata.amul.schema_version")
    assert _sent("sessionId")
    assert not _sent("metadata.agent.no_such_key")
    assert not _sent("metadata.no_such_key")
    assert not _sent("no_such_field")
