import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import voice_identity as identity


def test_jwt_phone_is_authoritative_and_normalized():
    resolved = identity.resolve_caller_identity(
        {"sub": "user-1", "phone": "+91 99244 57046"},
        "9924457046",
    )
    assert resolved.mobile == "9924457046"
    assert resolved.signed_in is True
    assert "9924457046" not in resolved.subject_id


def test_caller_supplied_phone_cannot_override_jwt():
    with pytest.raises(HTTPException) as exc:
        identity.resolve_caller_identity(
            {"sub": "user-1", "phone": "9924457046"},
            "9265866812",
        )
    assert exc.value.status_code == 403


def test_phone_parameter_is_rejected_when_token_has_no_phone_claim():
    with pytest.raises(HTTPException) as exc:
        identity.resolve_caller_identity({"sub": "7c477db0-807b-4ee0-a458-eed1dd3c5305"}, "9924457046")
    assert exc.value.status_code == 403


def test_session_binding_rejects_a_different_subject(monkeypatch):
    values = {}

    class FakeRedis:
        async def set(self, key, value, ex=None, nx=False):
            if nx and key in values:
                return None
            values[key] = value
            return True

        async def get(self, key):
            return values.get(key)

        async def expire(self, key, ttl):
            return key in values

    monkeypatch.setattr(identity, "redis_client", FakeRedis())
    first = identity.CallerIdentity(mobile="9924457046", subject_id="a" * 64, verified=True)
    other = identity.CallerIdentity(mobile="9265866812", subject_id="b" * 64, verified=True)

    asyncio.run(identity.bind_session_identity("session-1", first))
    asyncio.run(identity.bind_session_identity("session-1", first))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(identity.bind_session_identity("session-1", other))
    assert exc.value.status_code == 403

