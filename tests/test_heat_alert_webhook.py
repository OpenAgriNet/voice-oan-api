"""Focused tests for the partner heat-alert webhook.

Covers only the behaviours that matter:
  1. partner wire-key aliases (+ numeric coercion) on the request model
  2. shared-token auth + successful persist path on the HTTP endpoint
  3. retention cleanup deletes by ``received_at < now - retention`` (fresh kept)

No Postgres / network — DB session is faked. Style matches ``test_loan_eligibility``.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

# Avoid real external config side-effects during app import in CI/local.
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from app.config import settings
from app.models.heat_alert import HeatAlertWebhookRequest
from app.routers import webhooks
from app.tasks import webhook_cleanup_worker as cleanup
from main import app


PARTNER_PAYLOAD = {
    "alertID": "111",
    "deviceid": "S1IAD1869",
    "tag no": "1",
    "Pashuaadhar No": "1222244566743",
    "FarmName": "Demo Farm",
    "farmid": "797",
    "msg_title": "Heat detected",
    "msg_body": "Animal may be in heat",
    "heat_score": "89",
    "alert_type": "Amber",
    "notification_type": "HEAT",
    "AI Window start_time": "2026-09-16T09:40:39.000Z",
    "AI Window end_time": "2026-09-16T09:40:39.000Z",
    "alert_time": "1771411969",
    "timestamp": "2026-09-16T09:40:39.000Z",
    "society_code": "0007",
    "farmer_code": "1140",
    "farmer_contact": "9727703441",
    "system_tag_no": "12",
}


class _FakeSession:
    def __init__(self):
        self.added = []
        self.committed = False
        self.last_stmt = None
        self.rowcount = 0

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def execute(self, stmt):
        self.last_stmt = stmt
        return type("R", (), {"rowcount": self.rowcount})()


@contextlib.asynccontextmanager
async def _session_cm(session: _FakeSession):
    yield session


def test_partner_payload_aliases_and_numeric_coercion():
    """Partner keys with spaces/casing map correctly; numeric JSON becomes str."""
    parsed = HeatAlertWebhookRequest.model_validate(
        {
            "alertID": 111,
            "deviceid": "S1IAD1869",
            "tag no": 1,
            "Pashuaadhar No": "1222244566743",
            "FarmName": "Demo Farm",
            "farmid": 797,
            "heat_score": 89.0,
            "alert_type": "Amber",
            "AI Window start_time": "2026-09-16T09:40:39.000Z",
            "AI Window end_time": "2026-09-16T09:40:39.000Z",
            "timestamp": "2026-09-16T09:40:39.000Z",
            "farmer_contact": 9727703441,
        }
    )
    assert parsed.alert_id == "111"
    assert parsed.tag_no == "1"
    assert parsed.pashuaadhar_no == "1222244566743"
    assert parsed.farm_id == "797"
    assert parsed.heat_score == "89"
    assert parsed.alert_type == "Amber"
    assert parsed.ai_window_start_time == "2026-09-16T09:40:39.000Z"
    assert parsed.timestamp == "2026-09-16T09:40:39.000Z"
    assert parsed.farmer_contact == "9727703441"


def test_heat_alert_auth_and_persist(monkeypatch):
    """Missing/wrong token → 401; valid token persists event and returns 202."""
    session = _FakeSession()
    monkeypatch.setattr(webhooks, "webhook_db_configured", lambda: True)
    monkeypatch.setattr(webhooks, "get_webhook_session", lambda: _session_cm(session))
    monkeypatch.setattr(settings, "webhook_shared_token", "test-webhook-token")

    client = TestClient(app)

    assert (
        client.post("/api/webhooks/heat-alert", json=PARTNER_PAYLOAD).status_code == 401
    )
    assert (
        client.post(
            "/api/webhooks/heat-alert",
            json=PARTNER_PAYLOAD,
            headers={"X-Webhook-Token": "wrong"},
        ).status_code
        == 401
    )

    resp = client.post(
        "/api/webhooks/heat-alert",
        json=PARTNER_PAYLOAD,
        headers={"X-Webhook-Token": "test-webhook-token"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] is True
    assert body["id"]
    assert body["received_at"]

    assert session.committed is True
    assert len(session.added) == 1
    event = session.added[0]
    assert event.alert_id == "111"
    assert event.device_id == "S1IAD1869"
    assert event.tag_no == "1"
    assert event.pashuaadhar_no == "1222244566743"
    assert event.partner_alert_epoch_s == 1771411969
    assert event.ai_window_start_at is not None
    assert event.payload_raw["alertID"] == "111"
    assert event.payload_raw["tag no"] == "1"
    assert str(event.id) == body["id"]


def test_cleanup_deletes_only_rows_older_than_retention(monkeypatch):
    """Cleanup issues DELETE ... WHERE received_at < cutoff (= now - retention)."""
    session = _FakeSession()
    session.rowcount = 3
    monkeypatch.setattr(cleanup, "get_webhook_session", lambda: _session_cm(session))
    monkeypatch.setattr(cleanup.settings, "webhook_retention_hours", 24)

    before = datetime.now(timezone.utc)
    deleted = asyncio.run(cleanup._cleanup_once())
    after = datetime.now(timezone.utc)

    assert deleted == 3
    assert session.committed is True
    assert session.last_stmt is not None

    compiled = session.last_stmt.compile()
    sql = str(compiled).lower()
    assert "delete from heat_alert_webhook_events" in sql
    assert "received_at" in sql

    # Bind param is the cutoff; must sit ~24h in the past so fresh rows are kept.
    params = compiled.params
    assert len(params) == 1
    cutoff = next(iter(params.values()))
    assert isinstance(cutoff, datetime)
    expected = before - timedelta(hours=24)
    # Allow a small clock skew window around the cleanup call.
    assert expected - timedelta(seconds=2) <= cutoff <= after - timedelta(hours=24) + timedelta(seconds=2)
