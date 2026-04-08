"""In-memory session store for the Flask API.

Used by the new `/upload` → `/extract` → `/simulate-stream` flow to tie
multi-request state together. Each HTTP session gets a UUID-keyed entry
with:

    * `uploaded_files`: list of paths to user-uploaded docs (PDFs / text /
      images) that were written to a tmp dir by `/upload`
    * `event_proposal`: the structured EventProposal dict returned by
      `/extract`
    * `personas_proposed`: list of persona partial dicts (editable
      partials surfaced to the UI)
    * `stream_payloads`: a separate dict keyed by `stream_id` — these are
      transient POST bodies for `/simulate-stream/init` that wait for the
      `EventSource` GET to come pick them up

This store is deliberately process-local + in-memory. It is fine for the
single-instance demo deployment. A multi-worker production deploy would
need to swap to Redis — keep the API surface minimal so that's a drop-in.

TTL: entries are garbage-collected on each access after `ttl_seconds`.
Default is 30 minutes for sessions, 60 seconds for stream payloads
(they're meant to be consumed within seconds of being registered).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


log = logging.getLogger(__name__)


DEFAULT_SESSION_TTL_SECONDS = 30 * 60  # 30 minutes
DEFAULT_STREAM_PAYLOAD_TTL_SECONDS = 60  # short — streams must start quickly


@dataclass
class SessionState:
    """Per-user session state held across HTTP calls."""

    session_id: str
    created_at: float
    last_accessed: float
    uploaded_files: list[dict[str, Any]] = field(default_factory=list)
    event_proposal: dict[str, Any] | None = None
    personas_proposed: list[dict[str, Any]] = field(default_factory=list)
    prompt: str = ""
    urls: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamPayload:
    """A pending payload waiting for an `EventSource` GET to pick it up."""

    stream_id: str
    session_id: str | None
    payload: dict[str, Any]
    created_at: float
    claimed: bool = False


class SessionStore:
    """Thread-safe in-memory session + stream-payload store with TTL GC.

    All public methods take the lock exactly once. GC is lazy: it runs on
    every mutation and every lookup, scanning for expired entries.
    """

    def __init__(
        self,
        session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
        stream_ttl_seconds: float = DEFAULT_STREAM_PAYLOAD_TTL_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, SessionState] = {}
        self._streams: dict[str, StreamPayload] = {}
        self._session_ttl = session_ttl_seconds
        self._stream_ttl = stream_ttl_seconds

    # ─────────────────────── Sessions ───────────────────────

    def create_session(self) -> SessionState:
        now = time.time()
        session_id = uuid.uuid4().hex
        state = SessionState(
            session_id=session_id,
            created_at=now,
            last_accessed=now,
        )
        with self._lock:
            self._gc_locked(now)
            self._sessions[session_id] = state
        log.debug("session created: %s", session_id)
        return state

    def get_session(self, session_id: str) -> SessionState | None:
        now = time.time()
        with self._lock:
            self._gc_locked(now)
            state = self._sessions.get(session_id)
            if state is None:
                return None
            state.last_accessed = now
            return state

    def update_session(
        self,
        session_id: str,
        **fields: Any,
    ) -> SessionState | None:
        """Merge-update known fields on an existing session.

        Returns the updated state or None if the session does not exist.
        Unknown field names are silently ignored to keep call sites loose.
        """
        now = time.time()
        with self._lock:
            self._gc_locked(now)
            state = self._sessions.get(session_id)
            if state is None:
                return None
            for key, value in fields.items():
                if hasattr(state, key):
                    setattr(state, key, value)
            state.last_accessed = now
            return state

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    # ─────────────────────── Stream payloads ───────────────────────

    def register_stream_payload(
        self,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> str:
        """Register a streaming-sim payload; returns the `stream_id` that
        the client should use to open the SSE connection."""
        now = time.time()
        stream_id = uuid.uuid4().hex
        entry = StreamPayload(
            stream_id=stream_id,
            session_id=session_id,
            payload=dict(payload),
            created_at=now,
        )
        with self._lock:
            self._gc_locked(now)
            self._streams[stream_id] = entry
        log.debug("stream payload registered: %s", stream_id)
        return stream_id

    def claim_stream_payload(self, stream_id: str) -> StreamPayload | None:
        """Atomically mark the payload as claimed and return it. A payload
        can only be claimed once — subsequent calls return None. This
        prevents replay if a client retries the SSE GET."""
        now = time.time()
        with self._lock:
            self._gc_locked(now)
            entry = self._streams.get(stream_id)
            if entry is None or entry.claimed:
                return None
            entry.claimed = True
            return entry

    def peek_stream_payload(self, stream_id: str) -> StreamPayload | None:
        """Non-destructive lookup — used by tests and diagnostics."""
        now = time.time()
        with self._lock:
            self._gc_locked(now)
            return self._streams.get(stream_id)

    def delete_stream_payload(self, stream_id: str) -> bool:
        with self._lock:
            return self._streams.pop(stream_id, None) is not None

    # ─────────────────────── Introspection ───────────────────────

    def session_count(self) -> int:
        with self._lock:
            self._gc_locked(time.time())
            return len(self._sessions)

    def stream_payload_count(self) -> int:
        with self._lock:
            self._gc_locked(time.time())
            return len(self._streams)

    # ─────────────────────── Internal GC ───────────────────────

    def _gc_locked(self, now: float) -> None:
        """Drop expired entries. Caller must hold `self._lock`."""
        if self._sessions:
            stale_sessions = [
                sid
                for sid, s in self._sessions.items()
                if now - s.last_accessed > self._session_ttl
            ]
            for sid in stale_sessions:
                log.debug("session expired: %s", sid)
                self._sessions.pop(sid, None)

        if self._streams:
            stale_streams = [
                sid
                for sid, s in self._streams.items()
                if now - s.created_at > self._stream_ttl
            ]
            for sid in stale_streams:
                log.debug("stream payload expired: %s", sid)
                self._streams.pop(sid, None)


# ─────────────────────── Module-level singleton ───────────────────────
#
# The Flask API uses a single shared store per process. Tests can construct
# their own SessionStore instances directly.

_default_store: SessionStore | None = None


def get_default_store() -> SessionStore:
    global _default_store
    if _default_store is None:
        _default_store = SessionStore()
    return _default_store


def reset_default_store() -> None:
    """Drop the module-level singleton. Only used by tests."""
    global _default_store
    _default_store = None


__all__ = [
    "DEFAULT_SESSION_TTL_SECONDS",
    "DEFAULT_STREAM_PAYLOAD_TTL_SECONDS",
    "SessionState",
    "StreamPayload",
    "SessionStore",
    "get_default_store",
    "reset_default_store",
]
