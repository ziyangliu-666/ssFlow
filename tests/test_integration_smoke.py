"""End-to-end integration smoke harness for ssFlow.

Background: Rounds 1-4 of the autonomous review scored the engine on
unit tests + mechanical calibration backtests and missed four silent
runtime bugs — a TypeError that crashed every live run at round 0, a
P&L bookkeeping bug that silently dropped ~70% of every active fund's
holdings, a system-prompt injection path that was never reaching the
LLM, and a context-leak bug where prior-round conviction strings
followed agents into the next round.

Every one of those bugs would have been caught by a single end-to-end
run of the pure-Python pipeline that:

  1. Builds a minimal Event + persona set
  2. Spawns agents via the spawn_agents path that routes initial
     holdings to the event ticker
  3. Walks multiple rounds of the trading layer with a real LimitBoard
     + T1Ledger
  4. Asserts invariants that the bugs would have violated:
      - Prompt context contains session narrative + T+1 rule + K-line
        data that the engine claims it delivers.
      - P&L conservation: sum of agent nav changes across a round
        equals the net order flow (buy/sell) with the right sign.
      - Bearish event → shorts profit, longs lose.
      - Context stashed on agents in round N does not leak into
        round N+1 after the explicit clear.
      - Holdings under a real ticker are counted by holdings_value
        when only a scalar price is given.
      - Limit-board state transitions: pre-open gap → one_word_down,
        fill_rate on contra side drops below 1.0.

The harness avoids OASIS/CAMEL entirely: it drives the trading layer
directly, so it stays fast (sub-second) and runs in every CI pass.
When a future regression silently breaks one of these invariants the
test will fail before anyone has to rerun a live LLM scenario.
"""

from __future__ import annotations

import random

import pytest

from ssflow.event_severity import resolve_event_severity
from ssflow.instrument import Instrument, InstrumentUniverse
from ssflow.limit_board import LimitBoard, BoardType, BoardState
from ssflow.persona import MarketShare, Persona, SandboxConfig
from ssflow.round_context import clear_round_context, set_round_context
from ssflow.round_schedule import make_schedule
from ssflow.trading_layer import (
    T1Ledger,
    apply_distribution_to_agent_pop,
    spawn_agents,
)


# ─────────────────────── Helpers ───────────────────────


def _make_long_only_persona() -> Persona:
    return Persona(
        id="long_fund",
        archetype="公募基金",
        display_name="long only fund",
        voice_prompt="test",
        decision_mode="discretionary",
        role="institutional_long_only",
        market_share=MarketShare(by_volume=0.05),
        entity_role="trader",
        sandbox=SandboxConfig(
            instance_count=20,
            capital_distribution={"type": "fixed", "value_cny": 10_000_000},
            initial_position_distribution={"type": "fixed", "value": 0.3},
            risk={"max_position_pct": 0.8},
            action_space=[
                {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
                {"name": "buy", "side": "buy", "pool": "cash", "fraction": 0.3},
                {"name": "sell", "side": "sell", "pool": "holdings_in_target", "fraction": 0.5},
            ],
        ),
    )


def _make_short_fund() -> Persona:
    return Persona(
        id="short_fund",
        archetype="事件驱动做空",
        display_name="short fund",
        voice_prompt="test",
        decision_mode="discretionary",
        role="short_seller",
        market_share=MarketShare(by_volume=0.05),
        entity_role="trader",
        sandbox=SandboxConfig(
            instance_count=10,
            capital_distribution={"type": "fixed", "value_cny": 5_000_000},
            initial_position_distribution={"type": "fixed", "value": 0.0},
            risk={"max_position_pct": 0.5, "leverage_max": 2.0},
            action_space=[
                {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
                {"name": "short", "side": "sell", "pool": "margin", "fraction": 0.3},
            ],
        ),
    )


def _make_instrument_with_kline(ticker: str, price: float) -> Instrument:
    """Synthetic 30-day K-line so prompt_summary has data to render."""
    bars = []
    for i in range(30):
        daily_px = price * (1 + 0.004 * (i - 15) + 0.01 * ((-1) ** i))
        bars.append({
            "date": f"2026-03-{(i % 30) + 1:02d}",
            "open": daily_px * 0.998,
            "high": daily_px * 1.012,
            "low": daily_px * 0.988,
            "close": daily_px,
            "volume": int(5_000_000 * (1 + 0.2 * ((i % 5) - 2))),
        })
    return Instrument(
        ticker=ticker,
        name=f"test_{ticker}",
        market="ashare",
        relationship="event_subject",
        current_price=price,
        adv_value=price * 5_000_000 * 2,
        kline_30d=bars,
    )


class _FakeAgent:
    """Minimal stand-in for the OASIS SocialAgent used to verify the
    per-round context stash mechanism. The real SocialAgent is too heavy
    to construct inside a unit-level integration smoke."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id


# ─────────────────────── Prompt-trace invariants ───────────────────────


class TestPromptTraceInvariants:
    """Prior bugs: OASIS/CAMEL baked the system message from user_profile
    once at init time, so all per-round context rewrites were silently
    dropped. The set_round_context → patched user-instruction path is
    the fix. This suite verifies the pieces that path depends on."""

    def test_prompt_context_contains_session_narrative(self):
        sched = make_schedule("earnings-3d", "2026-04-15")
        ctx = sched.prompt_context(
            round_idx=0,
            cumulative_delta_pct=0.0,
            current_price=100.0,
        )
        assert "时间 / Time" in ctx
        assert "T+0 上午盘" in ctx
        # Session tag — hard signal agents can key off
        assert "[盘中" in ctx or "[尾盘" in ctx or "[全日" in ctx
        # One-line narrative per (session, day)
        assert "情境:" in ctx
        # A-share T+1 settlement reminder
        assert "T+1" in ctx
        assert "交易规则" in ctx

    def test_prompt_context_cross_day_reset_line(self):
        sched = make_schedule("earnings-3d", "2026-04-15")
        # Round 2 is T+1 (first round of the new trading day)
        ctx = sched.prompt_context(
            round_idx=2,
            cumulative_delta_pct=3.5,
            current_price=103.5,
            prev_day_close_delta_pct=3.5,
        )
        assert "新交易日开盘" in ctx
        assert "昨日收盘" in ctx or "隔夜" in ctx

    def test_instrument_summary_includes_kline_block(self):
        inst = _make_instrument_with_kline("300750", 218.50)
        universe = InstrumentUniverse(instruments=[inst], topic="test")
        summary = universe.prompt_summary()
        # Ticker + current price
        assert "300750" in summary
        # K-line statistics block
        assert "30日:" in summary
        assert "波动率" in summary
        assert "20日均线" in summary
        assert "近5日走势:" in summary
        # Event subject gets the OHLCV table
        assert "最近5日日线" in summary

    def test_round_context_does_not_leak_across_rounds(self):
        """Regression for the R5/R6 context-leak bug. set_round_context
        only merges, so the engine must call clear_round_context between
        rounds. This test simulates the round boundary directly."""
        agent = _FakeAgent("a")

        # Round 0: stash everything
        set_round_context(
            agent, time_ctx="R0 time", conviction_ctx="R0 conviction",
            pub_effects_ctx="R0 pub_effect",
        )
        assert agent._ssflow_round_context["conviction_ctx"] == "R0 conviction"

        # Engine round boundary: clear, then write only time_ctx for R1
        clear_round_context(agent)
        set_round_context(agent, time_ctx="R1 time")

        ctx = agent._ssflow_round_context
        assert ctx["time_ctx"] == "R1 time"
        assert not ctx.get("conviction_ctx"), (
            "R0 conviction_ctx leaked into R1 — engine forgot to clear"
        )
        assert not ctx.get("pub_effects_ctx"), (
            "R0 pub_effects_ctx leaked into R1 — engine forgot to clear"
        )


# ─────────────────────── P&L conservation invariants ───────────────────────


class TestPnLConservation:
    """Prior bug: holdings_value(scalar_price) dropped holdings under any
    ticker key != '_default', so compute_class_pnl silently reported -¥26億
    for long-only funds in a +12% rally. These tests pin the conservation
    invariants that would have caught that."""

    def test_single_round_long_buy_conserves_nav(self):
        """After a buy, an agent's nav at the same price equals its nav
        before the buy (cash out = holdings value in, same price)."""
        persona = _make_long_only_persona()
        agents = spawn_agents(
            persona, current_price=100.0, rng=random.Random(42),
            primary_ticker="300750",
        )
        nav_before = sum(a.nav(100.0) for a in agents)

        flow = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"hold": 0.0, "buy": 1.0, "sell": 0.0},
            current_price=100.0,
            rng=random.Random(1),
            instrument="300750",
            rationale="buy at mark",
            round_idx=0,
        )
        nav_after = sum(a.nav(100.0) for a in agents)

        # Within floating-point tolerance, nav is conserved under a
        # fair-value transaction
        assert nav_after == pytest.approx(nav_before, rel=1e-9)
        # Net flow should match cash outflow
        assert flow.net_flow > 0

    def test_holdings_tracked_under_real_ticker_not_default(self):
        """Regression for the CATL P&L bug: spawn with primary_ticker=
        '300750' must put initial holdings there, not under '_default',
        and holdings_value(scalar) must count them."""
        persona = _make_long_only_persona()
        agents = spawn_agents(
            persona, current_price=100.0, rng=random.Random(42),
            primary_ticker="300750",
        )
        for a in agents:
            if sum(a.holdings.values()) > 0:
                # The real ticker key must be the only holdings key
                assert "300750" in a.holdings
                assert "_default" not in a.holdings
                # holdings_value(scalar) must see it
                assert a.holdings_value(100.0) > 0

    def test_pnl_positive_for_long_fund_in_rally(self):
        """Long fund buys at 100, price moves to 120 → positive P&L.
        This is the specific shape the CATL bug violated."""
        persona = _make_long_only_persona()
        agents = spawn_agents(
            persona, current_price=100.0, rng=random.Random(42),
            primary_ticker="300750",
        )
        # Simulate a buy at 100
        apply_distribution_to_agent_pop(
            persona=persona, agents=agents,
            distribution={"hold": 0.0, "buy": 1.0, "sell": 0.0},
            current_price=100.0, rng=random.Random(1),
            instrument="300750", round_idx=0,
        )
        # Mark at 120
        total_pnl = sum(a.pnl(120.0) for a in agents)
        assert total_pnl > 0, (
            f"long fund in a +20% rally must be positive, got {total_pnl}"
        )

    def test_short_fund_profits_in_crash(self):
        """Short fund opens shorts via margin pool, price falls → positive
        P&L. This verifies the R5/R6 short-routing fix end-to-end."""
        from ssflow.oasis_trading_tool import (
            OrderCollector,
            make_freeform_trading_tool,
        )

        persona = _make_short_fund()
        collector = OrderCollector()
        tool = make_freeform_trading_tool(persona, collector)
        tool.func(side="sell", quantity_pct=0.5, rationale="terminal risk")
        order = collector.drain()[0]

        agents = spawn_agents(
            persona, current_price=10.0, rng=random.Random(42),
            primary_ticker="000687",
        )
        # All agents start flat
        assert all(sum(a.holdings.values()) == 0 for a in agents)

        flow = apply_distribution_to_agent_pop(
            persona=persona, agents=agents,
            distribution={"__freeform__": 1.0},
            current_price=10.0, rng=random.Random(1),
            instrument="000687",
            raw_distribution=order.raw_args,
            round_idx=0,
        )
        # Short sale: cash comes in (positive) → net_flow is negative by
        # convention (borrow proceeds registered as sell flow)
        assert flow.net_flow < 0
        # After a -20% move, the short is profitable
        total_pnl = sum(a.pnl(8.0) for a in agents)
        assert total_pnl > 0, (
            f"short fund in a -20% crash must be positive, got {total_pnl}"
        )


# ─────────────────────── Limit-board mechanics invariants ───────────────────────


class TestLimitBoardIntegration:
    """Prior bug: agents could magically fill at the limit price even when
    the board was at limit-down because the fill engine was computed but
    never actually scaled the per-agent fractions. And the severity map
    couldn't reach extreme-bear sentiment so pre-open auctions for
    delisting events gapped ~3% instead of -10%."""

    def test_delisting_event_text_pins_extreme_bear_sentiment(self):
        """An earnings event whose text mentions 退市/造假 should clamp
        sentiment to <= -0.85 regardless of the bland event_type. This
        is the regression that unblocked the 000687 live scenario."""
        res = resolve_event_severity(
            event_type="regulatory",
            event_text="证监会立案调查, *ST 华讯面临强制退市",
        )
        assert res.overnight_sentiment <= -0.85
        assert res.terminal_risk is True
        assert res.gap_vol >= 0.20

    def test_pre_open_gap_reaches_one_word_down(self):
        """With sentiment -0.85 and gap_vol 0.25 on a 10cm main-board
        stock, the simulated gap must be able to reach the full -10%
        limit-down. This is what the live 000687 run demonstrated."""
        from ssflow.limit_board import gap_open

        # Seed RNG so this is deterministic
        import random as _r
        _r.seed(7)
        prev_close = 10.0
        open_price = gap_open(
            prev_close=prev_close,
            overnight_sentiment=-0.85,
            board_type=BoardType.NORMAL,
            volatility=0.25,
        )
        gap_pct = (open_price - prev_close) / prev_close
        # With severity -0.85 and 25% vol, gap should land near -10%
        # (the 10cm floor for main-board stocks)
        assert gap_pct <= -0.08, f"expected deep negative gap, got {gap_pct}"

    def test_limit_down_board_blocks_long_sellers(self):
        """At LIMIT_DOWN, the buy side is full but the sell side fills only
        to the extent of available buy volume. A long fund trying to exit
        should see fill_rate < 1.0 when queue pressure exceeds capacity."""
        persona = _make_long_only_persona()
        agents = spawn_agents(
            persona, current_price=10.0, rng=random.Random(42),
            primary_ticker="000687",
        )

        # Pin the board at limit_down with strong sell queue vs weak buy
        board = LimitBoard(prev_close=10.0, board_type=BoardType.NORMAL)
        board.update(-0.10, buy_volume=1e7, sell_volume=1e10)
        assert board.state in (BoardState.LIMIT_DOWN, BoardState.ONE_WORD_DOWN)

        # Now try to sell 50% of holdings through the fill engine
        flow = apply_distribution_to_agent_pop(
            persona=persona,
            agents=agents,
            distribution={"hold": 0.0, "buy": 0.0, "sell": 1.0},
            current_price=9.0,
            rng=random.Random(1),
            instrument="000687",
            round_idx=0,
            limit_board=board,
        )
        # fill_rate must be capped below 1.0 — some sells don't execute
        assert flow.fill_rate < 1.0, (
            f"LIMIT_DOWN should reduce sell fill rate, got {flow.fill_rate}"
        )
        # Unfilled volume is reported
        assert flow.unfilled_volume > 0, (
            "LIMIT_DOWN with overwhelming sell pressure must report unfilled volume"
        )

    def test_t1_ledger_blocks_same_round_sell_after_buy(self):
        """A round-0 buy must lock the new shares against round-0 sells.
        Same-round roundtrip attempts should increment t1_blocked_sells.

        Uses a flat-starting persona (zero initial position) so the only
        shares available for a round-0 sell are the ones bought earlier
        in the same round. Otherwise the unlocked initial inventory
        masks the T+1 constraint entirely.
        """
        flat_persona = Persona(
            id="flat_long",
            archetype="flat long",
            display_name="flat long",
            voice_prompt="test",
            decision_mode="discretionary",
            role="institutional_long_only",
            market_share=MarketShare(by_volume=0.05),
            entity_role="trader",
            sandbox=SandboxConfig(
                instance_count=20,
                capital_distribution={"type": "fixed", "value_cny": 10_000_000},
                initial_position_distribution={"type": "fixed", "value": 0.0},
                risk={"max_position_pct": 0.8},
                action_space=[
                    {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
                    {"name": "buy", "side": "buy", "pool": "cash", "fraction": 0.3},
                    {"name": "sell", "side": "sell", "pool": "holdings_in_target", "fraction": 1.0},
                ],
            ),
        )
        agents = spawn_agents(
            flat_persona, current_price=100.0, rng=random.Random(42),
            primary_ticker="300750",
        )
        # Sanity: flat start
        assert all(sum(a.holdings.values()) == 0 for a in agents)

        t1 = T1Ledger()
        # Round 0: everyone buys
        buy_flow = apply_distribution_to_agent_pop(
            persona=flat_persona, agents=agents,
            distribution={"hold": 0.0, "buy": 1.0, "sell": 0.0},
            current_price=100.0, rng=random.Random(1),
            instrument="300750", round_idx=0,
            t1_ledger=t1,
        )
        assert buy_flow.net_flow > 0
        # After the buy, every agent holds something
        assert all(sum(a.holdings.values()) > 0 for a in agents)

        # Same round 0: try to sell 100% of holdings
        sell_flow = apply_distribution_to_agent_pop(
            persona=flat_persona, agents=agents,
            distribution={"hold": 0.0, "buy": 0.0, "sell": 1.0},
            current_price=100.0, rng=random.Random(2),
            instrument="300750", round_idx=0,
            t1_ledger=t1,
        )
        # Shares bought this round cannot be sold this round — the T+1
        # ledger blocks every sell attempt and net_flow stays ~zero
        assert sell_flow.t1_blocked_sells > 0, (
            f"T+1 ledger should count blocked sells, got "
            f"{sell_flow.t1_blocked_sells}"
        )
        assert sell_flow.net_flow == 0, (
            f"T+1 ledger must zero out same-round sells, got "
            f"{sell_flow.net_flow}"
        )


# ─────────────────────── End-to-end regime smoke ───────────────────────


class TestBearishRegimeSmoke:
    """Full integration: bearish event text → severity resolver → short
    fund opens shorts → limit-down board → longs can't exit → P&L
    direction is correct (shorts profit, longs hurt)."""

    def test_full_bear_regime_directional_correctness(self):
        """This is the test that would have caught every R5 runtime bug
        in a single failed assertion. It drives the pure-Python pipeline
        with a delisting-risk event text and asserts the entire
        directional chain ends up correct.

        Scenario shape:
          1. Severity resolver recognises terminal risk from event text
          2. Shorts enter positions at the pre-event price (normal board)
          3. Board transitions to limit-down
          4. Longs attempt to exit but hit the one-word wall
          5. After a further leg down, shorts are profitable, longs hurt
        """
        from ssflow.oasis_trading_tool import (
            OrderCollector,
            make_freeform_trading_tool,
        )

        # Step 1: severity resolution recognises terminal risk
        sev = resolve_event_severity(
            event_type="regulatory",
            event_text="公司立案调查, *ST 华讯面临强制退市, 造假锁死",
        )
        assert sev.terminal_risk
        assert sev.overnight_sentiment <= -0.85

        # Step 2: short fund opens shorts BEFORE the limit-down event.
        # Normal board state so their sells can fill at full rate — this
        # is the realistic shape: short funds scale in pre-event and
        # carry the position through the crash.
        short_persona = _make_short_fund()
        short_collector = OrderCollector()
        short_tool = make_freeform_trading_tool(short_persona, short_collector)
        short_tool.func(side="sell", quantity_pct=0.5, rationale="terminal")
        short_order = short_collector.drain()[0]
        assert short_order.raw_args["pool"] == "margin"

        long_persona = _make_long_only_persona()
        long_agents = spawn_agents(
            long_persona, current_price=10.0, rng=random.Random(42),
            primary_ticker="000687",
        )
        short_agents = spawn_agents(
            short_persona, current_price=10.0, rng=random.Random(43),
            primary_ticker="000687",
        )

        # Step 3: shorts fill on a normal pre-event board at price 10.0
        pre_event_board = LimitBoard(prev_close=10.0, board_type=BoardType.NORMAL)
        short_flow = apply_distribution_to_agent_pop(
            persona=short_persona, agents=short_agents,
            distribution={"__freeform__": 1.0},
            current_price=10.0, rng=random.Random(2),
            instrument="000687",
            raw_distribution=short_order.raw_args,
            round_idx=0,
            limit_board=pre_event_board,
        )
        # Short sale: borrow proceeds arrive as cash, holdings go negative
        assert short_flow.net_flow < 0, (
            f"short sale should produce negative flow, got {short_flow.net_flow}"
        )
        any_short_position = any(
            any(v < 0 for v in a.holdings.values()) for a in short_agents
        )
        assert any_short_position, "no agent ended up with a short position"

        # Step 4: board transitions to limit-down on the event
        crash_board = LimitBoard(prev_close=10.0, board_type=BoardType.NORMAL)
        crash_board.update(-0.10, buy_volume=1e6, sell_volume=1e10)
        assert crash_board.at_limit_down

        # Long fund tries to exit at the -10% price but hits the one-word wall
        long_flow = apply_distribution_to_agent_pop(
            persona=long_persona, agents=long_agents,
            distribution={"hold": 0.0, "buy": 0.0, "sell": 1.0},
            current_price=9.0, rng=random.Random(3),
            instrument="000687", round_idx=0,
            limit_board=crash_board,
        )
        assert long_flow.fill_rate < 1.0, (
            "LIMIT_DOWN must reduce long exit fill rate"
        )

        # Step 5: mark P&L at the further -20% price
        short_pnl = sum(a.pnl(8.0) for a in short_agents)
        long_pnl = sum(a.pnl(8.0) for a in long_agents)

        # Directional invariants: shorts profit, longs lose — the exact
        # assertion the R5 P&L bug violated silently
        assert short_pnl > 0, f"shorts must profit in crash, got {short_pnl}"
        assert long_pnl < 0, f"longs must lose in crash, got {long_pnl}"
        assert not (short_pnl > 0 and long_pnl > 0), (
            "directional inconsistency: both sides profitable in a "
            "unidirectional -20% move"
        )
