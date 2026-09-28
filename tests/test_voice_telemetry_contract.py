"""Voice traces must match telemetry/contracts/<schema version>.json.

A released contract never changes. Any change to what a turn sends (a key or an
outcome added, renamed or removed, a key inside a metadata block, a trace field,
the root name, or what a key means) goes out as a new schema version with its
own contract file, because traces already in Langfuse follow the old one.
"""

import ast
import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path

import langfuse

from app.config import settings
from app.services.telemetry_stamps import VOICE_TELEMETRY_SCHEMA_VERSION
from app.services.voice_trace import VoiceTrace

REPO = Path(__file__).resolve().parents[1]
CONTRACTS = REPO / "telemetry" / "contracts"
CONTRACT = CONTRACTS / f"{VOICE_TELEMETRY_SCHEMA_VERSION}.json"
CONTRACT_NAME = f"telemetry/contracts/{VOICE_TELEMETRY_SCHEMA_VERSION}.json"
NEW_VERSION = (
    f"{VOICE_TELEMETRY_SCHEMA_VERSION} can't change once released, so this needs a new schema version: "
    "bump VOICE_TELEMETRY_SCHEMA_VERSION in app/services/telemetry_stamps.py, copy "
    f"{CONTRACT_NAME} to the new version's file and make the change there, and add the new version to "
    f"telemetry/mappings/voice.yaml in amul-oan-api (it can extend {VOICE_TELEMETRY_SCHEMA_VERSION})."
)

# What each released contract holds, as a fingerprint of its content. Key order,
# spacing and the "note" text don't count. Add a version here when it ships.
RELEASED_CONTRACTS = {
    "voice.turn.v1": "a64e7ea4707a172a2efc90cc06e2c66962f6009109fff6cb2e9e6f300f89db85",
}

# Same rule as the chat contracts in amul-oan-api: names say what they are.
# error.type predates the rule and stays as it is in voice.turn.v1.
_GENERIC_NAMES = {"data", "id", "result", "status", "time", "type", "value"}
_GRANDFATHERED_NAMES = {"error.type"}
_SNAKE_CASE = re.compile(r"[a-z][a-z0-9_]*")


def _contract():
    assert CONTRACT.exists(), f"No contract for {VOICE_TELEMETRY_SCHEMA_VERSION}. Add {CONTRACT_NAME}."
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def _fingerprint(path):
    contract = json.loads(path.read_text(encoding="utf-8"))
    contract.pop("note", None)
    return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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


class _FakeLangfuse:
    """Records what VoiceTrace hands to Langfuse for the root of one turn."""

    def __init__(self):
        self.sent = {}

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        self.sent.setdefault("open", kwargs)
        yield self

    @contextmanager
    def propagate_attributes(self, **kwargs):
        self.sent["propagate"] = kwargs
        yield

    def update(self, **kwargs):
        self.sent["update"] = kwargs

    def end(self):
        pass


def _send_a_turn(monkeypatch):
    client = _FakeLangfuse()
    monkeypatch.setattr(langfuse, "propagate_attributes", client.propagate_attributes)
    monkeypatch.setattr(settings, "voice_trace_text_mode", "preview_hash")
    trace = VoiceTrace(
        session_id="session-redacted",
        user_id="<redacted-user-id>",
        source_lang="gu",
        target_lang="gu",
        query="<redacted question>",
        enabled=False,
    )
    trace.enabled, trace.langfuse_client = True, client
    with trace.request_context():
        trace.set_route("agent")
        trace.set_moderation(None)
        trace.set_pretranslation(text="<redacted question>", provider="<redacted-provider>", fallback_used=False)
        trace.set_farmer_context()
        trace.set_agent(signed_in=True, output="<redacted answer>")
        trace.record_emit("<redacted answer>")
        trace.finish(error=RuntimeError("<redacted>"))
    return client.sent


def _emitted_keys(monkeypatch):
    return set(_send_a_turn(monkeypatch)["update"]["metadata"]) | _keys_written_in_app()


def _outcomes_in(nodes):
    """Strings passed as the outcome to set_outcome() or finish(), positionally or as outcome=."""
    outcomes = set()
    for node in nodes:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"set_outcome", "finish"}:
            values = list(node.args) + [keyword.value for keyword in node.keywords if keyword.arg == "outcome"]
            for value in values:
                outcomes.update(
                    n.value for n in ast.walk(value) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                )
    return outcomes


def _outcomes_in_app():
    return _outcomes_in(_app_nodes())


def test_contract_matches_the_stamped_schema_version():
    assert _contract()["schema_version"] == VOICE_TELEMETRY_SCHEMA_VERSION


def test_released_contracts_never_change():
    for version, fingerprint in RELEASED_CONTRACTS.items():
        path = CONTRACTS / f"{version}.json"
        assert path.exists(), f"{version} is released: keep telemetry/contracts/{version}.json, old traces follow it."
        assert _fingerprint(path) == fingerprint, (
            f"telemetry/contracts/{version}.json is released and can't change: traces already in Langfuse "
            f"follow it. Undo the edit and put the change in a new version. {NEW_VERSION}"
        )


def test_names_are_specific_snake_case():
    contract = _contract()
    names = (
        contract["trace_fields"]
        + contract["metadata_keys"]
        + [f"{block}.{key}" for block, keys in contract["nested_keys"].items() for key in keys]
    )
    unclear = [
        name
        for name in names
        if name not in _GRANDFATHERED_NAMES
        and name != "amul.schema_version"
        and (
            not all(_SNAKE_CASE.fullmatch(part) for part in name.split("."))
            or name.split(".")[-1] in _GENERIC_NAMES
        )
    ]

    assert not unclear, (
        f"Unclear key names {unclear}. Use lowercase snake_case that says what the value is, "
        f"e.g. error_type rather than type, and never {sorted(_GENERIC_NAMES)} on their own."
    )


def test_no_contract_key_was_renamed_or_removed(monkeypatch):
    missing = set(_contract()["metadata_keys"]) - _emitted_keys(monkeypatch)

    assert not missing, f"Voice traces no longer send {sorted(missing)}. {NEW_VERSION}"


def test_turns_are_still_sent_on_the_contract_root(monkeypatch):
    sent = _send_a_turn(monkeypatch)
    root = _contract()["root"]
    names = {sent["open"]["name"], sent["propagate"]["trace_name"]}

    assert names == {root}, (
        f"Voice turns are now sent as {sorted(names)}, not {root!r}. Readers find turns by this name. "
        f"{NEW_VERSION} Set the new root there too."
    )


def test_no_trace_field_was_dropped(monkeypatch):
    sent = _send_a_turn(monkeypatch)
    fields = {
        "input": sent["open"].get("input"),
        "output": sent["update"].get("output"),
        "session_id": sent["propagate"].get("session_id"),
        "user_id": sent["propagate"].get("user_id"),
    }
    missing = [field for field in _contract()["trace_fields"] if not fields.get(field)]

    assert not missing, f"Voice traces no longer send the trace fields {missing}. {NEW_VERSION}"


def test_no_key_inside_a_block_was_renamed_or_removed(monkeypatch):
    metadata = _send_a_turn(monkeypatch)["update"]["metadata"]
    changed = {}
    for block, keys in _contract()["nested_keys"].items():
        sent = set(metadata[block]) if isinstance(metadata.get(block), dict) else set()
        if missing := set(keys) - sent:
            changed[block] = sorted(missing)

    assert not changed, f"Voice traces no longer send these keys inside metadata blocks: {changed}. {NEW_VERSION}"


def test_no_new_key_inside_a_block(monkeypatch):
    metadata = _send_a_turn(monkeypatch)["update"]["metadata"]
    added = {}
    for block, keys in _contract()["nested_keys"].items():
        sent = set(metadata[block]) if isinstance(metadata.get(block), dict) else set()
        if extra := sent - set(keys):
            added[block] = sorted(extra)

    assert not added, f"New keys inside metadata blocks: {added}. {NEW_VERSION}"


def test_new_keys_are_listed_in_the_contract(monkeypatch):
    extra = _emitted_keys(monkeypatch) - set(_contract()["metadata_keys"])

    assert not extra, f"New metadata keys {sorted(extra)}. {NEW_VERSION}"


def test_outcomes_match_the_contract():
    listed = set(_contract()["outcomes"])
    found = _outcomes_in_app()

    assert found <= listed, (
        f"New outcomes {sorted(found - listed)}. {NEW_VERSION} Also give each one a bucket in "
        "voice_outcome_vocabulary in telemetry/eras.yaml (amul-oan-api), or the adapters count it as unclassified."
    )
    assert listed <= found, f"Outcomes no longer emitted: {sorted(listed - found)}. {NEW_VERSION}"


def test_outcomes_are_found_however_they_are_passed():
    code = """
trace.set_outcome("stale_request")
trace.finish(outcome="hold_message")
trace.finish(trace.outcome or "error", error=RuntimeError("not an outcome"))
"""

    assert _outcomes_in(ast.walk(ast.parse(code))) == {"stale_request", "hold_message", "error"}
