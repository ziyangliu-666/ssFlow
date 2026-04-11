"""End-to-end smoke test for the ssFlow pipeline.

Unlike `test_api_streaming.py` (which mocks extract_event + run_simulation),
this test runs the REAL pipeline against a REAL LLM:

    1. Write a small BYD earnings .txt to tmp_path
    2. ingest_many → build_corpus
    3. extract_event with the corpus (Stage 0a classify + 0c synthesize —
       Stage 0b web research is skipped because the corpus is pre-fetched)
    4. propose_personas against the base ashare pack
    5. Pick 2 trader partials, rebuild them with persona_from_partial
    6. run_simulation with n_rounds=2, event_sink=ListSink
    7. Assert the event sequence is well-formed and includes the key types
    8. Assert Gap 2 is fixed: within every round, persona_thought events
       appear BEFORE trade_submitted events
    9. render_simulation_safe_or_quarantine → non-empty markdown
   10. insert_sandbox_simulation → get_simulation → assert round-trip

This test is gated with `@pytest.mark.e2e`. It only runs when
`SSFLOW_RUN_E2E=1` is set. Typical cost: ~$0.05-0.10. Wall time: ~60-120s.

    SSFLOW_RUN_E2E=1 .venv/bin/python -m pytest tests/test_e2e_smoke.py -v -s
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest


log = logging.getLogger(__name__)


BYD_EARNINGS_TEXT = """\
比亚迪股份 (002594.SZ) 发布 2026 年第一季度财报

主要数据:
- 营业收入: 1820 亿元人民币, 同比增长 +18.2%, 高于市场一致预期 +12% 的水平
- 归母净利润: 98 亿元人民币, 同比增长 +9.5%
- 汽车业务毛利率: 19.7%, 环比下降 2.3 个百分点, 低于市场预期的 22.5%
- 乘用车销量: 78.2 万辆, 同比增长 +15.1%
- 新能源车型占比: 96%

管理层解读:
公司在业绩发布会上表示, 毛利率环比下降主要由于原材料成本短期压力和
新车型爬坡阶段的一次性摊销。预计二季度随着规模效应释放, 毛利率将逐步
修复。公司维持全年销量目标不变, 约 380 万辆。

市场反应背景:
过去 10 个交易日, 比亚迪股价累计上涨约 +5%, 反映对一季度业绩的乐观预期。
A 股新能源车板块整体动能良好, 宁德时代、理想汽车等同期上涨幅度约 +4% 至 +8%。

当前收盘价: 218.50 元
20 日平均成交额: 约 85 亿元人民币
"""


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_full_pipeline_byd_earnings(tmp_path: Path):
    """Run the full extract → propose → simulate → report pipeline end-to-end.

    This is the canonical "does the whole thing work?" test. If this passes,
    the Flask layer (which is just plumbing around the same calls) will work.
    """
    # Lazy imports AFTER conftest has set up the env
    from ssflow.config import settings
    from ssflow.document_ingestion import build_corpus, ingest_many
    from ssflow.event_bus import (
        EVENT_PERSONA_THOUGHT,
        EVENT_PRICE_UPDATED,
        EVENT_ROUND_COMPLETE,
        EVENT_ROUND_START,
        EVENT_SIMULATION_COMPLETE,
        EVENT_SIMULATION_START,
        EVENT_TRADE_SUBMITTED,
        ListSink,
    )
    from ssflow.event_extractor import extract_event
    from ssflow.oasis_engine import run_simulation
    from ssflow.persona import load_personas, persona_set_hash
    from ssflow.persona_proposer import persona_from_partial, propose_personas
    from ssflow.report import render_simulation_safe_or_quarantine, save_report
    from ssflow.scorecard import (
        get_simulation,
        init_db,
        insert_sandbox_simulation,
    )

    init_db()

    # 1. Write the sample input
    input_file = tmp_path / "byd_q1_2026.txt"
    input_file.write_text(BYD_EARNINGS_TEXT, encoding="utf-8")
    log.info("[e2e] wrote input file: %s (%d chars)", input_file, len(BYD_EARNINGS_TEXT))

    # 2. Ingest the file → build corpus
    ingested = await ingest_many(files=[input_file])
    assert len(ingested) == 1
    doc = ingested[0]
    assert doc.kind == "text"
    assert "比亚迪" in doc.text
    assert doc.char_count > 100
    log.info("[e2e] ingested 1 doc: %d chars", doc.char_count)

    corpus = build_corpus(ingested)
    assert len(corpus) == 1
    assert "比亚迪" in corpus[0]["excerpt"]

    # 3. Stage 0 extraction with the real LLM
    prompt = (
        "分析比亚迪 Q1 2026 财报: 营收 beat 但毛利率 miss, "
        "市场会怎么反应?"
    )
    log.info("[e2e] running extract_event (this hits the real LLM)")
    proposal = await extract_event(prompt, pre_fetched_corpus=corpus)

    # Sanity-check the proposal
    assert proposal.ticker, f"ticker empty in proposal: {proposal}"
    assert proposal.instrument, f"instrument empty: {proposal}"
    assert proposal.event_type, f"event_type empty: {proposal}"
    assert proposal.event_date, f"event_date empty: {proposal}"
    assert proposal.event_text, f"event_text empty: {proposal}"

    # The LLM should pick A-share / BYD / earnings-type given the input
    assert proposal.market in ("ashare", "a-share", "cn-equity"), (
        f"expected ashare market, got {proposal.market}"
    )
    log.info(
        "[e2e] extracted: market=%s ticker=%s instrument=%s",
        proposal.market, proposal.ticker, proposal.instrument,
    )

    event = proposal.to_event()

    # Build a one-element InstrumentUniverse with the BYD price/ADV
    # baked into the sample text. The engine reads price/ADV from here,
    # not from the event itself.
    from ssflow.instrument import Instrument, InstrumentUniverse
    instrument_universe = InstrumentUniverse(
        instruments=[
            Instrument(
                ticker=event.ticker,
                name=event.instrument or event.ticker,
                market=event.market or "ashare",
                relationship="event_subject",
                current_price=218.50,
                adv_value=8.5e9,
            )
        ],
        topic=event.event_text[:200],
    )

    # 4. Propose personas against the ashare pack
    base_pack_path = proposal.suggested_personas_path or "personas/ashare.yaml"
    # Resolve to an absolute path under personas/
    resolved = settings.project_root / base_pack_path
    if not resolved.exists():
        resolved = settings.project_root / "personas" / "ashare.yaml"
    assert resolved.exists(), f"persona pack not found: {resolved}"

    partials = propose_personas(base_pack_path=resolved)
    assert len(partials) >= 2, f"expected at least 2 personas, got {len(partials)}"
    log.info("[e2e] proposed %d partials from %s", len(partials), resolved)

    # 5. Pick exactly 2 trader partials — cheapest possible sim (2 agents × 2 rounds)
    trader_partials = [p for p in partials if p.get("entity_role") == "trader"][:2]
    assert len(trader_partials) == 2, (
        f"need 2 trader partials, got {len(trader_partials)}"
    )

    # Downsize each persona to 10 agents to keep the sim fast + cheap
    for partial in trader_partials:
        partial["instance_count"] = 10

    base_pack = load_personas(resolved)
    personas = [
        persona_from_partial(p, base_pack=base_pack) for p in trader_partials
    ]
    assert len(personas) == 2

    # 6. Run the real simulation with 2 rounds
    sink = ListSink()
    log.info("[e2e] running run_simulation (2 rounds, 2 traders, real LLM)")
    result = await run_simulation(
        event,
        personas,
        n_rounds=2,
        seed=42,
        event_sink=sink,
        instrument_universe=instrument_universe,
    )
    log.info(
        "[e2e] sim done: %d rounds, price %.2f → %.2f, cost $%.4f, elapsed %.1fs",
        result.n_rounds, result.initial_price, result.final_price,
        result.cost_usd, result.elapsed_seconds,
    )

    assert result.n_rounds == 2
    assert result.initial_price == pytest.approx(218.50)
    assert isinstance(result.final_price, float)
    assert result.final_price > 0

    # 7. Event sequence checks
    types = sink.types_in_order()
    log.info("[e2e] event types emitted: %s", types)

    # Exactly one simulation_start + exactly one simulation_complete
    assert types.count(EVENT_SIMULATION_START) == 1, types
    assert types.count(EVENT_SIMULATION_COMPLETE) == 1, types

    # One round_start + round_complete + price_updated per round
    assert types.count(EVENT_ROUND_START) == 2, types
    assert types.count(EVENT_ROUND_COMPLETE) == 2, types
    assert types.count(EVENT_PRICE_UPDATED) == 2, types

    # Simulation start is first, complete is last
    assert types[0] == EVENT_SIMULATION_START
    assert types[-1] == EVENT_SIMULATION_COMPLETE

    # 8. Gap 2 assertion: within each round, persona_thought events appear
    # BEFORE trade_submitted events (thoughts precede trades causally).
    for round_idx in (0, 1):
        round_events = _extract_round_events(sink.events, round_idx)
        round_types = [e.get("type") for e in round_events]

        thought_positions = [
            i for i, t in enumerate(round_types) if t == EVENT_PERSONA_THOUGHT
        ]
        trade_positions = [
            i for i, t in enumerate(round_types) if t == EVENT_TRADE_SUBMITTED
        ]

        # It's OK if a round has zero thoughts (LLM might only tool-call
        # without posting) but if BOTH exist, thoughts must come first.
        if thought_positions and trade_positions:
            assert max(thought_positions) < min(trade_positions), (
                f"Gap 2 regression in round {round_idx}: "
                f"thoughts at {thought_positions}, trades at {trade_positions} — "
                f"expected all thoughts to precede all trades. "
                f"Round type sequence: {round_types}"
            )
            log.info(
                "[e2e] round %d: %d thoughts (all before) %d trades ✓",
                round_idx, len(thought_positions), len(trade_positions),
            )
        else:
            log.info(
                "[e2e] round %d: %d thoughts, %d trades (partial)",
                round_idx, len(thought_positions), len(trade_positions),
            )

    # 9. Render the markdown report
    markdown, compliance_ok = render_simulation_safe_or_quarantine(result)
    assert compliance_ok, "report failed compliance filter"
    assert len(markdown) > 200, f"markdown suspiciously short: {len(markdown)} chars"
    assert "ssFlow" in markdown or "Simulation" in markdown

    report_path = save_report(markdown, result.simulation_id)
    assert report_path.exists()
    log.info("[e2e] report saved: %s (%d chars)", report_path, len(markdown))

    # 10. Persist to the scorecard → round-trip via get_simulation
    class_pnl = result.compute_class_pnl()
    sim_id = insert_sandbox_simulation(
        event_ticker=event.ticker,
        event_date=event.event_date,
        event_type=event.event_type,
        event_text_hash=event.text_hash,
        persona_set_hash=persona_set_hash(personas),
        model_default=settings.default_model,
        seed=settings.seed,
        n_personas=result.n_personas,
        n_rounds=result.n_rounds,
        initial_price=result.initial_price,
        final_price=result.final_price,
        cumulative_delta_pct=result.cumulative_delta_pct,
        price_trajectory=result.price_trajectory,
        class_pnl=class_pnl,
        strategic_signals=None,
        lambda_used=result.lambda_used,
        adv_used=result.adv_value_used,
        full_report_path=str(report_path),
        cost_usd=result.cost_usd,
        elapsed_seconds=result.elapsed_seconds,
        simulation_id=result.simulation_id,
        llm_seed=result.llm_seed,
        publication_log=None,
        oasis_db_path=result.oasis_db_path,
    )
    assert sim_id == result.simulation_id

    row = get_simulation(sim_id)
    assert row is not None
    assert row["simulation_id"] == sim_id
    assert row["event_ticker"] == event.ticker
    assert row["n_rounds"] == 2
    assert row["initial_price"] == pytest.approx(result.initial_price)
    assert row["final_price"] == pytest.approx(result.final_price)
    assert row["price_trajectory_json"] is not None
    assert row["class_pnl_json"] is not None

    # 11. Hit GET /report/<sim_id> via the Flask test client against the
    # real sim we just inserted, and verify the structured JSON shape
    # (Gap 1 verification on a real payload).
    from api.app import create_app
    app = create_app()
    app.testing = True
    auth = {"X-Auth-Password": settings.flask_password.get_secret_value()}
    with app.test_client() as client:
        r = client.get(f"/report/{sim_id}", headers=auth)
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        assert body["simulation_id"] == sim_id
        assert body["markdown"] == markdown
        assert body["summary"] is not None
        assert body["summary"]["initial_price"] == pytest.approx(result.initial_price)
        assert body["summary"]["final_price"] == pytest.approx(result.final_price)
        assert body["summary"]["price_trajectory"] == result.price_trajectory
        # per_class_pnl sorted desc
        pcp = body["summary"]["per_class_pnl"]
        if len(pcp) >= 2:
            assert pcp[0]["pnl"] >= pcp[1]["pnl"]
        assert body["meta"]["event_ticker"] == event.ticker
        assert body["meta"]["n_rounds"] == 2
        assert body["meta"]["n_personas"] == 2

    log.info("[e2e] ✓ pipeline OK — sim_id=%s", sim_id)


def _extract_round_events(
    events: list[dict], round_idx: int
) -> list[dict]:
    """Pull the events belonging to a specific round, in emit order.

    `round_start` starts the window for its round_idx. `round_complete`
    closes it. Everything in between with a matching `round_idx` field
    belongs to that round. We also include events that don't carry a
    round_idx field IFF they sit between the round_start and round_complete
    of that round (e.g. market broadcaster posts).
    """
    in_round = False
    out: list[dict] = []
    for ev in events:
        t = ev.get("type")
        if t == "round_start" and ev.get("round_idx") == round_idx:
            in_round = True
            out.append(ev)
            continue
        if t == "round_complete" and ev.get("round_idx") == round_idx:
            out.append(ev)
            in_round = False
            continue
        if in_round:
            out.append(ev)
    return out


def test_report_endpoint_structured_json(tmp_path: Path):
    """Verify GET /report/<id> returns {markdown, summary, meta} JSON.

    This is cheap — no LLM calls. It inserts a fake simulation row into
    the (tmp-path-isolated) scorecard, writes a fake markdown report to
    the real reports/ dir, and hits the Flask test client to verify the
    endpoint glues them together. The report file is cleaned up on exit.
    """
    from ssflow.scorecard import init_db, insert_sandbox_simulation
    from ssflow.config import settings

    init_db()

    sim_id = "e2e_test_report_xyz"
    md = "# Test Report\n\nFinal price **232.91** (-3.5%)."
    report_path = settings.reports_dir / f"{sim_id}.md"
    report_path.write_text(md, encoding="utf-8")

    try:
        insert_sandbox_simulation(
            event_ticker="002594",
            event_date="2026-04-08",
            event_type="earnings_release",
            event_text_hash="abc123",
            persona_set_hash="def456",
            model_default="gpt-4o-mini",
            seed=42,
            n_personas=2,
            n_rounds=2,
            initial_price=241.35,
            final_price=232.91,
            cumulative_delta_pct=-0.0349,
            price_trajectory=[241.35, 237.62, 232.91],
            class_pnl={"retail_momentum": -68000000.0, "public_fund_pm": 72000000.0},
            strategic_signals=None,
            lambda_used=0.5,
            adv_used=8.5e9,
            full_report_path=str(report_path),
            cost_usd=0.042,
            elapsed_seconds=47.3,
            simulation_id=sim_id,
        )

        from api.app import create_app
        app = create_app()
        app.testing = True
        auth = {"X-Auth-Password": settings.flask_password.get_secret_value()}
        with app.test_client() as client:
            r = client.get(f"/report/{sim_id}", headers=auth)
            assert r.status_code == 200
            body = r.get_json()
            assert body is not None

            # Shape
            assert body["simulation_id"] == sim_id
            assert "markdown" in body
            assert "summary" in body
            assert "meta" in body

            # Summary content
            summary = body["summary"]
            assert summary["initial_price"] == pytest.approx(241.35)
            assert summary["final_price"] == pytest.approx(232.91)
            assert summary["delta_pct"] == pytest.approx(-0.0349)
            assert len(summary["price_trajectory"]) == 3
            assert len(summary["per_class_pnl"]) == 2
            # Winning class is the one with the highest P&L
            assert summary["winning_class"]["pnl"] == pytest.approx(72000000.0)
            # Net flow total is the sum
            assert summary["net_flow_total"] == pytest.approx(4000000.0)
            # per_class_pnl is sorted desc
            assert summary["per_class_pnl"][0]["pnl"] >= summary["per_class_pnl"][1]["pnl"]

            # Meta content
            meta = body["meta"]
            assert meta["event_ticker"] == "002594"
            assert meta["n_rounds"] == 2
            assert meta["cost_usd"] == pytest.approx(0.042)

            # The markdown body is echoed back unchanged
            assert body["markdown"] == md

            # ?format=md returns raw markdown
            r_md = client.get(f"/report/{sim_id}?format=md", headers=auth)
            assert r_md.status_code == 200
            assert r_md.data.decode("utf-8") == md
            assert r_md.headers["Content-Type"].startswith("text/markdown")
    finally:
        # Clean up the report file we wrote to the real reports/ dir.
        if report_path.exists():
            report_path.unlink()


def test_report_endpoint_unknown_id_returns_404():
    """GET /report/<unknown> should still return 404 cleanly."""
    from api.app import create_app
    from ssflow.config import settings

    app = create_app()
    app.testing = True
    auth = {"X-Auth-Password": settings.flask_password.get_secret_value()}
    with app.test_client() as client:
        r = client.get("/report/does_not_exist_xyz_123", headers=auth)
        assert r.status_code == 404
        body = r.get_json()
        assert body["error"] == "not_found"
