"""Integration tests for the new ssFlow API surface.

Covers:
  * /session   — session creation
  * /upload    — multipart file upload
  * /extract   — runs the full pipeline against a mocked event_extractor
  * /personas/template — blank partial returned
  * /simulate-stream/init  — payload registration + stream_id issuance
  * /simulate-stream/<id>  — SSE stream produced by a fake engine that
    emits canned events instead of running real OASIS

The Flask test client cannot consume streaming responses incrementally,
but it CAN drain the iterator after the request is "done", which is
sufficient to assert the SSE event types and payloads.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from ssflow.config import settings


AUTH_HEADER = {"X-Auth-Password": settings.flask_password.get_secret_value()}


@pytest.fixture
def client():
    from api.app import create_app
    from ssflow.session_store import reset_default_store

    reset_default_store()
    app = create_app()
    app.testing = True
    with app.test_client() as c:
        yield c
    reset_default_store()


# ─────────────────────── Health + auth ───────────────────────


def test_healthz_open(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_session_requires_auth(client):
    r = client.post("/session")
    assert r.status_code == 401


def test_session_create(client):
    r = client.post("/session", headers=AUTH_HEADER)
    assert r.status_code == 200
    body = r.get_json()
    assert "session_id" in body
    assert isinstance(body["session_id"], str)
    assert len(body["session_id"]) > 0


# ─────────────────────── /upload ───────────────────────


def test_upload_saves_files(client, tmp_path):
    sess_resp = client.post("/session", headers=AUTH_HEADER)
    session_id = sess_resp.get_json()["session_id"]

    file_data = (io.BytesIO(b"hello world\n"), "note.txt")

    r = client.post(
        "/upload",
        headers=AUTH_HEADER,
        data={"session_id": session_id, "files": file_data},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["session_id"] == session_id
    assert len(body["files"]) == 1
    saved = body["files"][0]
    assert saved["name"] == "note.txt"
    assert saved["size"] == len(b"hello world\n")
    assert Path(saved["tmp_path"]).exists()
    assert Path(saved["tmp_path"]).read_text(encoding="utf-8") == "hello world\n"


def test_upload_rejects_missing_files(client):
    sess_resp = client.post("/session", headers=AUTH_HEADER)
    session_id = sess_resp.get_json()["session_id"]
    r = client.post(
        "/upload",
        headers=AUTH_HEADER,
        data={"session_id": session_id},
        content_type="multipart/form-data",
    )
    assert r.status_code == 400


# ─────────────────────── /personas/template ───────────────────────


def test_personas_template_default(client):
    r = client.post("/personas/template", headers=AUTH_HEADER, json={})
    assert r.status_code == 200
    body = r.get_json()
    assert body["decision_mode"] == "discretionary"
    assert body["entity_role"] == "trader"
    assert body["instance_count"] == 500


def test_personas_template_with_decision_mode(client):
    r = client.post(
        "/personas/template",
        headers=AUTH_HEADER,
        json={"decision_mode": "systematic"},
    )
    assert r.status_code == 200
    assert r.get_json()["decision_mode"] == "systematic"


# ─────────────────────── /extract ───────────────────────


def _fake_proposal():
    """Build a fake EventProposal that the mocked extractor can return."""
    from ssflow.event_extractor import EventProposal

    return EventProposal(
        ticker="002594",
        instrument="BYD",
        market="ashare",
        event_type="earnings",
        event_date="2026-04-08",
        event_text="BYD Q1 营收 +18% beat 但毛利率 -2.3pp miss",
        prior_consensus="市场预期 +12%",
        recent_price_action="过去 10 日 +5%",
        sector_context="新能源车出货放量",
        current_price=218.5,
        adv_value=8e9,
        price_currency="CNY",
        confidence={"market": 0.95, "instrument": 0.95, "ticker": 0.95},
        suggested_personas_path="personas/ashare.yaml",
        sources_consulted=[],
        extraction_warnings=[],
    )


def test_extract_with_prompt_only(client):
    sess_resp = client.post("/session", headers=AUTH_HEADER)
    session_id = sess_resp.get_json()["session_id"]

    async def fake_extract(raw_input, *, pre_fetched_corpus=None):
        return _fake_proposal()

    with patch("api.app.extract_event", side_effect=fake_extract):
        r = client.post(
            "/extract",
            headers=AUTH_HEADER,
            json={
                "session_id": session_id,
                "prompt": "BYD Q1 earnings 出炉",
            },
        )
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["session_id"] == session_id
    assert body["event_proposal"]["ticker"] == "002594"
    assert body["base_personas_path"].endswith("ashare.yaml")
    assert isinstance(body["personas_proposed"], list)
    assert len(body["personas_proposed"]) > 0
    # Each partial has the editable fields
    p = body["personas_proposed"][0]
    for key in ("id", "archetype", "voice_prompt", "_base_template"):
        assert key in p


def test_extract_rejects_empty_input(client):
    sess_resp = client.post("/session", headers=AUTH_HEADER)
    session_id = sess_resp.get_json()["session_id"]
    r = client.post(
        "/extract",
        headers=AUTH_HEADER,
        json={"session_id": session_id, "prompt": ""},
    )
    assert r.status_code == 400
    assert r.get_json()["error"] == "empty_input"


def test_extract_with_uploaded_files(client, tmp_path):
    sess_resp = client.post("/session", headers=AUTH_HEADER)
    session_id = sess_resp.get_json()["session_id"]

    file_data = (io.BytesIO("BYD 2026 Q1 财报: 营收 +18%, 毛利率 -2.3pp".encode("utf-8")), "byd.md")
    client.post(
        "/upload",
        headers=AUTH_HEADER,
        data={"session_id": session_id, "files": file_data},
        content_type="multipart/form-data",
    )

    captured = {}

    async def fake_extract(raw_input, *, pre_fetched_corpus=None):
        captured["raw_input"] = raw_input
        captured["corpus"] = pre_fetched_corpus
        return _fake_proposal()

    with patch("api.app.extract_event", side_effect=fake_extract):
        r = client.post(
            "/extract",
            headers=AUTH_HEADER,
            json={"session_id": session_id, "prompt": "Analyze the BYD report"},
        )
    assert r.status_code == 200, r.get_data(as_text=True)
    assert captured["corpus"] is not None
    assert any("BYD" in entry["excerpt"] for entry in captured["corpus"])


# ─────────────────────── /simulate-stream/init ───────────────────────


def _real_partial_for_init(client):
    """Pull a real persona partial out of /extract so /init has valid input."""
    sess = client.post("/session", headers=AUTH_HEADER).get_json()["session_id"]

    async def fake_extract(raw_input, *, pre_fetched_corpus=None):
        return _fake_proposal()

    with patch("api.app.extract_event", side_effect=fake_extract):
        r = client.post(
            "/extract",
            headers=AUTH_HEADER,
            json={"session_id": sess, "prompt": "test"},
        )
    body = r.get_json()
    return sess, body["personas_proposed"], body["event_proposal"]


def test_simulate_stream_init_returns_stream_id(client):
    sess, partials, event = _real_partial_for_init(client)
    # Use only trader partials so the resolved list always has flow.
    trader_partials = [p for p in partials if p["entity_role"] == "trader"][:2]
    assert len(trader_partials) >= 1

    r = client.post(
        "/simulate-stream/init",
        headers=AUTH_HEADER,
        json={
            "session_id": sess,
            "event": event,
            "personas": trader_partials,
            "n_rounds": 2,
            "seed": 42,
            "base_personas_path": "personas/ashare.yaml",
        },
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert "stream_id" in body
    assert isinstance(body["stream_id"], str)
    assert body["n_personas"] == len(trader_partials)


def test_simulate_stream_init_rejects_empty_personas(client):
    sess, _partials, event = _real_partial_for_init(client)
    r = client.post(
        "/simulate-stream/init",
        headers=AUTH_HEADER,
        json={
            "session_id": sess,
            "event": event,
            "personas": [],
            "n_rounds": 2,
        },
    )
    assert r.status_code == 400
    assert r.get_json()["error"] == "personas_required"


def test_simulate_stream_init_rejects_unready_event(client):
    sess, partials, event = _real_partial_for_init(client)
    bad_event = dict(event)
    bad_event["current_price"] = None  # makes it not sandbox-ready
    r = client.post(
        "/simulate-stream/init",
        headers=AUTH_HEADER,
        json={
            "session_id": sess,
            "event": bad_event,
            "personas": partials[:1],
            "n_rounds": 2,
        },
    )
    assert r.status_code == 400
    assert r.get_json()["error"] == "sandbox_not_ready"


# ─────────────────────── /simulate-stream/<id> SSE ───────────────────────


def _parse_sse(stream_bytes: bytes) -> list[dict]:
    """Best-effort SSE parser for the test client output."""
    text = stream_bytes.decode("utf-8", errors="replace")
    events: list[dict] = []
    for block in re.split(r"\n\n+", text):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        ev_type = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                ev_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except Exception:
            payload = {"raw": data_lines}
        payload["_type"] = ev_type
        events.append(payload)
    return events


def test_simulate_stream_emits_events_via_fake_engine(client):
    """Patch run_simulation with a fake that emits canned events through
    its event_sink, and confirm the SSE stream forwards them."""
    sess, partials, event = _real_partial_for_init(client)
    trader_partials = [p for p in partials if p["entity_role"] == "trader"][:2]
    init = client.post(
        "/simulate-stream/init",
        headers=AUTH_HEADER,
        json={
            "session_id": sess,
            "event": event,
            "personas": trader_partials,
            "n_rounds": 2,
            "seed": 42,
            "base_personas_path": "personas/ashare.yaml",
        },
    )
    stream_id = init.get_json()["stream_id"]

    from ssflow.event_bus import (
        EVENT_PRICE_UPDATED,
        EVENT_ROUND_COMPLETE,
        EVENT_ROUND_START,
        EVENT_SIMULATION_COMPLETE,
        EVENT_SIMULATION_START,
        safe_emit,
    )
    from ssflow.oasis_engine import OasisSimResult, RoundRecord

    async def fake_run_simulation(event, personas, *, n_rounds=None, event_sink=None, **_):
        # Emit a canned sequence of events
        safe_emit(event_sink, EVENT_SIMULATION_START, simulation_id="fake_sim_1",
                  initial_price=100.0, n_personas=len(personas), n_rounds=2)
        for i in range(2):
            safe_emit(event_sink, EVENT_ROUND_START, simulation_id="fake_sim_1",
                      round_idx=i, current_price=100.0 + i)
            safe_emit(event_sink, EVENT_PRICE_UPDATED, simulation_id="fake_sim_1",
                      round_idx=i, price_before=100.0 + i, price_after=100.5 + i,
                      delta_pct=0.005, net_flow_total=12345.0)
            safe_emit(event_sink, EVENT_ROUND_COMPLETE, simulation_id="fake_sim_1",
                      round_idx=i, publications_count=0, orders_count=0,
                      class_flows_count=len(personas), price_after=100.5 + i,
                      net_flow_total=12345.0)
        safe_emit(event_sink, EVENT_SIMULATION_COMPLETE, simulation_id="fake_sim_1",
                  initial_price=100.0, final_price=101.5,
                  cumulative_delta_pct=0.015, price_trajectory=[100.0, 100.5, 101.5],
                  n_rounds_completed=2, n_publications=0, elapsed_seconds=0.1, cost_usd=0.0)
        return OasisSimResult(
            simulation_id="fake_sim_1",
            event=event,
            personas=personas,
            initial_price=100.0,
            final_price=101.5,
            rounds=[
                RoundRecord(
                    round_idx=i,
                    price_before=100.0 + i,
                    price_after=100.5 + i,
                    delta_pct=0.005,
                    net_flow=12345.0,
                    class_flows=[],
                    publications_this_round=[],
                )
                for i in range(2)
            ],
            elapsed_seconds=0.1,
            cost_usd_at_start=0.0,
            cost_usd_at_end=0.0,
            llm_seed=42,
            lambda_used=0.5,
            adv_value_used=8e9,
            oasis_db_path="/dev/null",
        )

    with patch("api.app.run_simulation", side_effect=fake_run_simulation), \
         patch("api.app.save_report", return_value=Path("/tmp/fake_report.md")), \
         patch("api.app.insert_sandbox_simulation", return_value="sim_id_x"):
        r = client.get(f"/simulate-stream/{stream_id}", buffered=True)
        assert r.status_code == 200
        # Drain the SSE stream
        body = b"".join(r.response)

    events = _parse_sse(body)
    types = [e["_type"] for e in events]
    # Required milestones
    for required in (
        "simulation_start",
        "round_start",
        "price_updated",
        "round_complete",
        "simulation_complete",
        "simulation_done",
    ):
        assert required in types, f"missing event type {required} in {types}"


def test_simulate_stream_rejects_unknown_id(client):
    r = client.get("/simulate-stream/00000000000000000000000000000000")
    assert r.status_code == 404
