"""Voice traces must match telemetry/contracts/<schema version>.json.

Adding a key or an outcome only needs the contract file updated. Renaming or
removing a key, a key inside a metadata block, a trace field or the root name
needs a new schema version, because readers of older traces depend on it.
"""

import ast
import json
from contextlib import contextmanager
from pathlib import Path

import langfuse

from app.config import settings
from app.services.telemetry_stamps import VOICE_TELEMETRY_SCHEMA_VERSION
from app.services.voice_trace import VoiceTrace

REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "telemetry" / "contracts" / f"{VOICE_TELEMETRY_SCHEMA_VERSION}.json"
CONTRACT_NAME = f"telemetry/contracts/{VOICE_TELEMETRY_SCHEMA_VERSION}.json"
BUMP_THE_VERSION = (
    f"Readers of {VOICE_TELEMETRY_SCHEMA_VERSION} traces depend on it: bump VOICE_TELEMETRY_SCHEMA_VERSION in "
    "app/services/telemetry_stamps.py, add a contract file for the new version, and add it to "
    "telemetry/mappings/voice.yaml in amul-oan-api."
)


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


def test_no_contract_key_was_renamed_or_removed(monkeypatch):
    missing = set(_contract()["metadata_keys"]) - _emitted_keys(monkeypatch)

    assert not missing, f"Voice traces no longer send {sorted(missing)}. {BUMP_THE_VERSION}"


def test_turns_are_still_sent_on_the_contract_root(monkeypatch):
    sent = _send_a_turn(monkeypatch)
    root = _contract()["root"]
    names = {sent["open"]["name"], sent["propagate"]["trace_name"]}

    assert names == {root}, (
        f"Voice turns are now sent as {sorted(names)}, not {root!r}. Readers find turns by this name. "
        f"{BUMP_THE_VERSION} Set the new root there too."
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

    assert not missing, f"Voice traces no longer send the trace fields {missing}. {BUMP_THE_VERSION}"


def test_no_key_inside_a_block_was_renamed_or_removed(monkeypatch):
    metadata = _send_a_turn(monkeypatch)["update"]["metadata"]
    changed = {}
    for block, keys in _contract()["nested_keys"].items():
        sent = set(metadata[block]) if isinstance(metadata.get(block), dict) else set()
        if missing := set(keys) - sent:
            changed[block] = sorted(missing)

    assert not changed, f"Voice traces no longer send these keys inside metadata blocks: {changed}. {BUMP_THE_VERSION}"


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


def test_outcomes_are_found_however_they_are_passed():
    code = """
trace.set_outcome("stale_request")
trace.finish(outcome="hold_message")
trace.finish(trace.outcome or "error", error=RuntimeError("not an outcome"))
"""

    assert _outcomes_in(ast.walk(ast.parse(code))) == {"stale_request", "hold_message", "error"}
