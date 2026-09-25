"""Voice traces must match telemetry/contracts/<schema version>.json.

Adding a key or an outcome only needs the contract file updated. Renaming or
removing a key needs a new schema version, because readers of older traces
depend on it.
"""

import ast
import json
from pathlib import Path

from app.services import voice_trace
from app.services.telemetry_stamps import VOICE_TELEMETRY_SCHEMA_VERSION
from app.services.voice_trace import VoiceTrace

REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "telemetry" / "contracts" / f"{VOICE_TELEMETRY_SCHEMA_VERSION}.json"
CONTRACT_NAME = f"telemetry/contracts/{VOICE_TELEMETRY_SCHEMA_VERSION}.json"


def _contract():
    assert CONTRACT.exists(), f"No contract for {VOICE_TELEMETRY_SCHEMA_VERSION}. Add {CONTRACT_NAME}."
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def _app_nodes():
    for path in sorted((REPO / "app").rglob("*.py")):
        yield from ast.walk(ast.parse(path.read_text(encoding="utf-8")))


def _metadata_key(node):
    """The key in `<x>.metadata["key"]`, or None."""
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "metadata"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value
    return None


def _keys_written_in_app():
    keys = set()
    for node in _app_nodes():
        if isinstance(node, ast.Assign):
            keys.update(key for target in node.targets if (key := _metadata_key(target)))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and (key := _metadata_key(node.target)):
            keys.add(key)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "metadata"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            keys.add(node.args[0].value)
    return keys


def _keys_sent_by_a_finished_turn(monkeypatch):
    sent = {}
    monkeypatch.setattr(voice_trace, "_safe_update", lambda observation, **kwargs: sent.update(kwargs))
    trace = VoiceTrace(
        session_id="session-redacted",
        user_id="<redacted-user-id>",
        source_lang="gu",
        target_lang="gu",
        query="<redacted question>",
        enabled=False,
    )
    trace.set_route("agent")
    trace.record_emit("<redacted answer>")
    trace.finish(error=RuntimeError("<redacted>"))
    return set(sent["metadata"])


def _emitted_keys(monkeypatch):
    return _keys_sent_by_a_finished_turn(monkeypatch) | _keys_written_in_app()


def _outcomes_in_app():
    outcomes = set()
    for node in _app_nodes():
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"set_outcome", "finish"}:
            for arg in node.args:
                outcomes.update(
                    n.value for n in ast.walk(arg) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                )
    return outcomes


def test_contract_matches_the_stamped_schema_version():
    assert _contract()["schema_version"] == VOICE_TELEMETRY_SCHEMA_VERSION


def test_no_contract_key_was_renamed_or_removed(monkeypatch):
    missing = set(_contract()["metadata_keys"]) - _emitted_keys(monkeypatch)

    assert not missing, (
        f"Voice traces no longer send {sorted(missing)}. Renaming or removing a key breaks readers of "
        f"{VOICE_TELEMETRY_SCHEMA_VERSION} traces: bump VOICE_TELEMETRY_SCHEMA_VERSION in "
        "app/services/telemetry_stamps.py and add a contract file for the new version."
    )


def test_new_keys_are_listed_in_the_contract(monkeypatch):
    extra = _emitted_keys(monkeypatch) - set(_contract()["metadata_keys"])

    assert not extra, (
        f"New metadata keys {sorted(extra)}. Adding keys keeps the schema version; list them in {CONTRACT_NAME}."
    )


def test_outcomes_match_the_contract():
    listed = set(_contract()["outcomes"])
    found = _outcomes_in_app()

    assert found <= listed, (
        f"New outcomes {sorted(found - listed)}. Add them to {CONTRACT_NAME} and to voice_outcome_vocabulary "
        "in telemetry/eras.yaml (amul-oan-api), or the adapters will count them as unclassified."
    )
    assert listed <= found, (
        f"Outcomes no longer emitted: {sorted(listed - found)}. Remove them from {CONTRACT_NAME} and note "
        "the change in telemetry/eras.yaml once it ships."
    )
