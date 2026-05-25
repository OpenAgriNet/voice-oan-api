"""Unit tests for the stale-while-revalidate farmer cache: freshness intervals,
the Redis refresh queue, the bounded cold fetch, and the voice read policy.

Self-contained: uses asyncio.run + mocks, so it needs neither a live Redis nor
pytest-asyncio.
"""
import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import app.services.voice as voice
import agents.services.farmer_cache as fc

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_cold_import_has_no_circular_import():
    """Regression: `uvicorn main:app` imports the refresh worker before the
    routers, so farmer_cache must be importable COLD (first touch) without
    hitting the farmer_cache <-> agents.tools cycle. Run in a subprocess to get
    a truly fresh interpreter, mirroring the app's startup import order."""
    code = (
        "import app.tasks.farmer_refresh_worker;"
        "import agents.services.farmer_cache;"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import failed:\n{result.stderr}"
    assert "OK" in result.stdout


class _Env:
    """Duck-typed stand-in for FarmerDataEnvelope (only fields _compute_freshness reads)."""

    def __init__(self, lookup_status, age_hours):
        self.lookupStatus = lookup_status
        self.fetchedAt = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()


def test_freshness_intervals_found_vs_not_found():
    # found: soft expiry at 12h
    assert fc._compute_freshness(_Env("found", 6))[0] is False
    assert fc._compute_freshness(_Env("found", 13))[0] is True
    # not_found: shorter soft expiry at 2h
    assert fc._compute_freshness(_Env("not_found", 1))[0] is False
    assert fc._compute_freshness(_Env("not_found", 3))[0] is True


def test_interval_constants():
    assert fc.FARMER_REFRESH_INTERVAL == 60 * 60 * 12
    assert fc.FARMER_NEGATIVE_REFRESH_INTERVAL == 60 * 60 * 2
    assert fc.FARMER_CACHE_TTL == 60 * 60 * 24 * 7


def test_enqueue_pushes_phone_to_redis_set():
    fake_redis = AsyncMock()
    with patch.object(fc, "redis_client", fake_redis):
        asyncio.run(fc.enqueue_farmer_refresh("9999999999"))
    fake_redis.sadd.assert_awaited_once_with(fc.FARMER_REFRESH_QUEUE_KEY, "9999999999")


def test_enqueue_ignores_empty_phone():
    fake_redis = AsyncMock()
    with patch.object(fc, "redis_client", fake_redis):
        asyncio.run(fc.enqueue_farmer_refresh(""))
    fake_redis.sadd.assert_not_called()


def test_drain_pops_batch_and_refreshes_each():
    fake_redis = AsyncMock()
    fake_redis.spop.return_value = ["111", "222", "333"]
    with patch.object(fc, "redis_client", fake_redis), \
         patch.object(fc, "refresh_farmer_data", new=AsyncMock()) as refresh:
        processed = asyncio.run(fc.drain_farmer_refresh_queue_once(batch=10))
    assert processed == 3
    fake_redis.spop.assert_awaited_once_with(fc.FARMER_REFRESH_QUEUE_KEY, 10)
    assert refresh.await_count == 3


def test_drain_empty_queue_returns_zero():
    fake_redis = AsyncMock()
    fake_redis.spop.return_value = None
    with patch.object(fc, "redis_client", fake_redis), \
         patch.object(fc, "refresh_farmer_data", new=AsyncMock()) as refresh:
        processed = asyncio.run(fc.drain_farmer_refresh_queue_once())
    assert processed == 0
    refresh.assert_not_called()


def test_bounded_fetch_returns_envelope_on_success():
    sentinel = object()
    with patch.object(fc, "refresh_farmer_data", new=AsyncMock(return_value=sentinel)):
        result = asyncio.run(fc.refresh_farmer_data_bounded("111", timeout=1.0))
    assert result is sentinel


def test_bounded_fetch_times_out_and_enqueues():
    async def _slow(_phone):
        await asyncio.sleep(5)

    enqueue = AsyncMock()
    with patch.object(fc, "refresh_farmer_data", new=_slow), \
         patch.object(fc, "enqueue_farmer_refresh", new=enqueue):
        result = asyncio.run(fc.refresh_farmer_data_bounded("111", timeout=0.05))
    assert result is None
    enqueue.assert_awaited_once_with("111")


def test_voice_read_returns_cached_without_blocking():
    cached = _Env("found", 1)  # fresh, within max-serve-stale
    with patch.object(voice, "get_farmer_data_cached_only", new=AsyncMock(return_value=cached)), \
         patch.object(voice, "refresh_farmer_data_bounded", new=AsyncMock()) as bounded:
        result = asyncio.run(voice.get_or_fetch_farmer_data("111"))
    assert result is cached
    bounded.assert_not_called()


def test_voice_read_blocks_when_too_stale():
    stale = _Env("found", 1000)   # well beyond the 48h max-serve-stale
    fresh = _Env("found", 0)
    with patch.object(voice, "get_farmer_data_cached_only", new=AsyncMock(return_value=stale)), \
         patch.object(voice, "refresh_farmer_data_bounded", new=AsyncMock(return_value=fresh)) as bounded:
        result = asyncio.run(voice.get_or_fetch_farmer_data("111"))
    assert result is fresh
    bounded.assert_awaited_once_with("111")


def test_voice_read_too_stale_falls_back_to_stale_on_api_failure():
    stale = _Env("found", 1000)
    with patch.object(voice, "get_farmer_data_cached_only", new=AsyncMock(return_value=stale)), \
         patch.object(voice, "refresh_farmer_data_bounded", new=AsyncMock(return_value=None)):
        result = asyncio.run(voice.get_or_fetch_farmer_data("111"))
    assert result is stale  # API also failed -> serve stale rather than nothing


def test_exceeds_max_serve_stale():
    assert fc.exceeds_max_serve_stale(_Env("found", 1)) is False
    assert fc.exceeds_max_serve_stale(_Env("found", 1000)) is True
    assert fc.exceeds_max_serve_stale(None) is True


def test_refresh_keeps_found_on_transient_not_found():
    """A transient empty upstream response must not wipe a still-fresh 'found' record."""
    existing = _Env("found", 1)
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)   # lock acquired
    fake_redis.delete = AsyncMock()
    with patch.object(fc, "redis_client", fake_redis), \
         patch.object(fc, "fetch_farmer_info_raw", new=AsyncMock(return_value=[])), \
         patch.object(fc, "get_cached_farmer_data", new=AsyncMock(return_value=existing)), \
         patch.object(fc, "set_cached_farmer_data", new=AsyncMock()) as set_cache:
        result = asyncio.run(fc.refresh_farmer_data("9999999999"))
    assert result is existing
    set_cache.assert_not_called()  # did NOT downgrade to not_found


def test_record_api_trace_captures_body_status_and_reason():
    from agents.tools import farmer_animal_backends as backends

    class _Resp:
        status_code = 200
        text = '{"totalAnimals": 5}'

    captured = {}

    class _Obs:
        def update(self, output=None, metadata=None):
            captured["output"] = output
            captured["metadata"] = metadata

    with backends.fetch_reason("cold_fetch"):
        backends._record_api_trace(_Obs(), _Resp(), provider="amulpashudhan", url="http://x")
    assert captured["output"]["status_code"] == 200
    assert captured["output"]["ok"] is True
    assert captured["output"]["body"] == '{"totalAnimals": 5}'
    assert captured["output"]["fetch_reason"] == "cold_fetch"
    assert captured["metadata"] == {"provider": "amulpashudhan", "url": "http://x"}


def test_record_api_trace_none_observation_is_noop():
    from agents.tools import farmer_animal_backends as backends

    class _Resp:
        status_code = 500
        text = "boom"

    backends._record_api_trace(None, _Resp(), provider="x", url="y")  # must not raise


def test_fetch_reason_contextvar_default_and_scope():
    from agents.tools import farmer_animal_backends as backends

    assert backends.current_fetch_reason() == "request"
    with backends.fetch_reason("background_refresh"):
        assert backends.current_fetch_reason() == "background_refresh"
    assert backends.current_fetch_reason() == "request"


def test_voice_read_cold_miss_does_bounded_fetch():
    fetched = object()
    with patch.object(voice, "get_farmer_data_cached_only", new=AsyncMock(return_value=None)), \
         patch.object(voice, "refresh_farmer_data_bounded", new=AsyncMock(return_value=fetched)) as bounded:
        result = asyncio.run(voice.get_or_fetch_farmer_data("111"))
    assert result is fetched
    bounded.assert_awaited_once_with("111")
