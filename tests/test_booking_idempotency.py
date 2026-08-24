"""Voice booking tools must be idempotent per session so an agent re-run (the
OSS->managed streaming fallback re-executes tool calls) cannot double-book.
Mirror of amul-oan-api's test; voice tools take ctx (session_id + ensure_in_scope)."""

import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL_NAME", "gpt-test")

import asyncio
from types import SimpleNamespace

import pytest

from agents.tools import ai_call as ai_mod
from agents.tools import health_call as hc_mod
from agents.models.ai_call import AISpecies
from agents.models.health_call import HealthCaseType


def _ctx(session_id):
    async def _ensure_in_scope():
        return True

    account = SimpleNamespace(union_code="U", society_code="S", farmer_code="F")
    return SimpleNamespace(deps=SimpleNamespace(
        session_id=session_id,
        ensure_in_scope=_ensure_in_scope,
        farmer_unions=[],
        signed_in=True,
        identity_verified=True,
        mobile="9924457046",
        subject_id="subject-1",
        farmer_accounts=[account],
        ai_technician_ids=["tech1", "t"],
    ))


def _patch_cache(monkeypatch, module):
    """In-memory cache simulating Redis: add() is atomic SET-NX (raises if key
    exists), shared by try_reserve/release_reservation and the tool's set/get."""
    store = {}

    async def fake_add(key, value, ttl=None, namespace=None):
        k = (namespace, key)
        if k in store:
            raise ValueError("key exists")  # aiocache add == Redis SET NX
        store[k] = value
        return True

    async def fake_set(key, value, ttl=None, namespace=None):
        store[(namespace, key)] = value

    async def fake_get(key, namespace=None):
        return store.get((namespace, key))

    async def fake_delete(key, namespace=None):
        store.pop((namespace, key), None)

    monkeypatch.setattr(module.cache, "add", fake_add)
    monkeypatch.setattr(module.cache, "set", fake_set)
    monkeypatch.setattr(module.cache, "get", fake_get)
    monkeypatch.setattr(module.cache, "delete", fake_delete)
    monkeypatch.setenv("PASHUGPT_TOKEN", "tok")
    return store


def test_ai_call_idempotent_on_rerun(monkeypatch):
    _patch_cache(monkeypatch, ai_mod)
    calls = {"n": 0}

    async def fake_api(request, token):
        calls["n"] += 1
        return SimpleNamespace(ticket_number="T1", ait_name="AIT", model_dump=lambda: {"ticket_number": "T1"})

    monkeypatch.setattr(ai_mod, "create_ai_call_api", fake_api)
    species = next(iter(AISpecies))

    r1 = asyncio.run(ai_mod.create_ai_call(_ctx("s1"), "U", "S", "F", "tech1", species))
    r2 = asyncio.run(ai_mod.create_ai_call(_ctx("s1"), "U", "S", "F", "tech1", species))

    assert calls["n"] == 1
    assert "booked successfully" in r1
    assert "already" in r2.lower()


def test_health_call_idempotent_on_rerun(monkeypatch):
    _patch_cache(monkeypatch, hc_mod)
    calls = {"n": 0}

    async def fake_api(request, token):
        calls["n"] += 1
        return SimpleNamespace(ticket_number="H1")

    monkeypatch.setattr(hc_mod, "create_health_call_api", fake_api)
    species = next(iter(AISpecies))
    case_type = next(iter(HealthCaseType))

    # remark differs across the re-run (model output varies) — session key still dedupes
    r1 = asyncio.run(hc_mod.create_health_call(_ctx("s1"), "U", "S", "F", species, case_type, "remark v1"))
    r2 = asyncio.run(hc_mod.create_health_call(_ctx("s1"), "U", "S", "F", species, case_type, "remark v2"))

    assert calls["n"] == 1
    assert "booked successfully" in r1
    assert "already" in r2.lower()


def test_ai_call_concurrent_submits_book_once(monkeypatch):
    """Two concurrent submits for the same session (double-tap / retry) must
    result in exactly ONE booking — the atomic reservation closes the race."""
    _patch_cache(monkeypatch, ai_mod)
    calls = {"n": 0}

    async def fake_api(request, token):
        calls["n"] += 1
        await asyncio.sleep(0.02)  # booking latency — the window two requests race in
        return SimpleNamespace(ticket_number=f"T{calls['n']}", ait_name="AIT", model_dump=lambda: {})

    monkeypatch.setattr(ai_mod, "create_ai_call_api", fake_api)
    species = next(iter(AISpecies))

    async def go():
        return await asyncio.gather(
            ai_mod.create_ai_call(_ctx("sX"), "U", "S", "F", "t", species),
            ai_mod.create_ai_call(_ctx("sX"), "U", "S", "F", "t", species),
        )

    r1, r2 = asyncio.run(go())
    assert calls["n"] == 1
    assert any("booked successfully" in r for r in (r1, r2))
    assert any("already" in r.lower() for r in (r1, r2))


def test_ai_call_no_session_does_not_crash(monkeypatch):
    _patch_cache(monkeypatch, ai_mod)

    async def fake_api(request, token):
        return SimpleNamespace(ticket_number="T1", ait_name="AIT", model_dump=lambda: {})

    monkeypatch.setattr(ai_mod, "create_ai_call_api", fake_api)
    species = next(iter(AISpecies))
    r = asyncio.run(ai_mod.create_ai_call(_ctx(None), "U", "S", "F", "tech1", species))
    assert "booked successfully" in r
