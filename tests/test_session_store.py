"""Tests for `ssflow.session_store`."""

from __future__ import annotations

import threading
import time

from ssflow.session_store import (
    DEFAULT_SESSION_TTL_SECONDS,
    SessionStore,
    get_default_store,
    reset_default_store,
)


# ─────────────────────── Sessions ───────────────────────


def test_create_session_returns_unique_state():
    store = SessionStore()
    s1 = store.create_session()
    s2 = store.create_session()
    assert s1.session_id != s2.session_id
    assert store.session_count() == 2


def test_get_session_updates_last_accessed():
    store = SessionStore()
    state = store.create_session()
    original = state.last_accessed
    time.sleep(0.01)
    refreshed = store.get_session(state.session_id)
    assert refreshed is not None
    assert refreshed.last_accessed > original


def test_get_missing_session_returns_none():
    store = SessionStore()
    assert store.get_session("nonexistent") is None


def test_update_session_merges_fields():
    store = SessionStore()
    state = store.create_session()
    updated = store.update_session(
        state.session_id,
        prompt="hello",
        urls=["https://example.com"],
    )
    assert updated is not None
    assert updated.prompt == "hello"
    assert updated.urls == ["https://example.com"]


def test_update_session_ignores_unknown_fields():
    store = SessionStore()
    state = store.create_session()
    # Should not raise, should not attach unknown attr
    updated = store.update_session(state.session_id, bogus_field=42)
    assert updated is not None
    assert not hasattr(updated, "bogus_field")


def test_update_missing_session_returns_none():
    store = SessionStore()
    assert store.update_session("nonexistent", prompt="x") is None


def test_delete_session():
    store = SessionStore()
    state = store.create_session()
    assert store.delete_session(state.session_id) is True
    assert store.get_session(state.session_id) is None
    assert store.delete_session(state.session_id) is False


def test_session_ttl_expires_idle_entries():
    store = SessionStore(session_ttl_seconds=0.05)
    state = store.create_session()
    assert store.get_session(state.session_id) is not None
    time.sleep(0.08)
    # Next access triggers GC, which expires the stale entry.
    assert store.get_session(state.session_id) is None
    assert store.session_count() == 0


# ─────────────────────── Stream payloads ───────────────────────


def test_register_and_claim_stream_payload():
    store = SessionStore()
    stream_id = store.register_stream_payload({"event": {"ticker": "X"}})
    assert isinstance(stream_id, str) and len(stream_id) > 0

    entry = store.claim_stream_payload(stream_id)
    assert entry is not None
    assert entry.stream_id == stream_id
    assert entry.payload["event"]["ticker"] == "X"
    assert entry.claimed is True


def test_claim_is_one_shot():
    store = SessionStore()
    stream_id = store.register_stream_payload({"k": 1})

    first = store.claim_stream_payload(stream_id)
    second = store.claim_stream_payload(stream_id)

    assert first is not None
    assert second is None


def test_peek_does_not_claim():
    store = SessionStore()
    stream_id = store.register_stream_payload({"k": 2})

    peek1 = store.peek_stream_payload(stream_id)
    peek2 = store.peek_stream_payload(stream_id)
    assert peek1 is not None and peek2 is not None
    assert peek1.claimed is False

    # Claiming still works after peeking.
    claimed = store.claim_stream_payload(stream_id)
    assert claimed is not None and claimed.claimed is True


def test_stream_payload_ttl_expires():
    store = SessionStore(stream_ttl_seconds=0.05)
    stream_id = store.register_stream_payload({"k": 3})
    time.sleep(0.08)
    assert store.claim_stream_payload(stream_id) is None
    assert store.stream_payload_count() == 0


def test_delete_stream_payload():
    store = SessionStore()
    stream_id = store.register_stream_payload({"k": 4})
    assert store.delete_stream_payload(stream_id) is True
    assert store.claim_stream_payload(stream_id) is None
    assert store.delete_stream_payload(stream_id) is False


def test_register_with_session_id_attaches_reference():
    store = SessionStore()
    session = store.create_session()
    stream_id = store.register_stream_payload(
        {"k": 5}, session_id=session.session_id
    )
    entry = store.peek_stream_payload(stream_id)
    assert entry is not None
    assert entry.session_id == session.session_id


# ─────────────────────── Thread safety ───────────────────────


def test_store_is_thread_safe():
    store = SessionStore()
    created_ids: list[str] = []
    lock = threading.Lock()

    def worker():
        for _ in range(50):
            state = store.create_session()
            with lock:
                created_ids.append(state.session_id)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(created_ids) == 200
    assert len(set(created_ids)) == 200  # all unique


# ─────────────────────── Module singleton ───────────────────────


def test_get_default_store_returns_same_instance():
    reset_default_store()
    a = get_default_store()
    b = get_default_store()
    assert a is b
    reset_default_store()


def test_reset_default_store_clears_singleton():
    reset_default_store()
    a = get_default_store()
    reset_default_store()
    b = get_default_store()
    assert a is not b
    reset_default_store()


def test_default_session_ttl_is_thirty_minutes():
    assert DEFAULT_SESSION_TTL_SECONDS == 30 * 60
