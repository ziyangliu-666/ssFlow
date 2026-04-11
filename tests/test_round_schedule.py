"""Tests for RoundSchedule and time-aware round management."""

from ssflow.round_schedule import RoundDef, RoundSchedule, make_default_schedule


class TestRoundDef:
    def test_serializable(self):
        rd = RoundDef(
            id="T0_AM", label="T+0 上午盘",
            calendar_start="2026-04-09 09:30", calendar_end="2026-04-09 11:30",
            hours_since_event=0.0, active_agent_types=["retail", "kol"],
        )
        s = rd.to_serializable()
        assert s["id"] == "T0_AM"
        assert s["label"] == "T+0 上午盘"
        assert s["active_agent_types"] == ["retail", "kol"]


class TestRoundSchedule:
    def test_n_rounds(self):
        sched = make_default_schedule("2026-04-09", n_trading_days=5)
        assert sched.n_rounds == 6  # T0_AM + T0_PM + T1-T4

    def test_get_round(self):
        sched = make_default_schedule("2026-04-09")
        r0 = sched.get_round(0)
        assert r0.id == "T0_AM"
        assert r0.label == "T+0 上午盘"

    def test_get_round_out_of_bounds(self):
        sched = make_default_schedule("2026-04-09")
        assert sched.get_round(999) is None

    def test_prompt_context_first_round(self):
        sched = make_default_schedule("2026-04-09")
        ctx = sched.prompt_context(0)
        assert "T+0 上午盘" in ctx
        assert "0 小时" in ctx

    def test_prompt_context_later_round(self):
        sched = make_default_schedule("2026-04-09")
        ctx = sched.prompt_context(2)
        assert "T+1" in ctx
        assert "24 小时" in ctx
        assert "上一轮" in ctx

    def test_prompt_context_with_price_data(self):
        sched = make_default_schedule("2026-04-09")
        ctx = sched.prompt_context(2, cumulative_delta_pct=5.3, current_price=220.50)
        assert "+5.30%" in ctx
        assert "220.50" in ctx

    def test_serializable_roundtrip(self):
        sched = make_default_schedule("2026-04-09", n_trading_days=3)
        s = sched.to_serializable()
        sched2 = RoundSchedule.from_serializable(s)
        assert sched2.n_rounds == sched.n_rounds
        assert sched2.rounds[0].id == "T0_AM"
        assert sched2.event_datetime == "2026-04-09"

    def test_active_agent_types_vary(self):
        sched = make_default_schedule("2026-04-09", n_trading_days=5)
        # T+0 AM: retail heavy
        assert "retail" in sched.rounds[0].active_agent_types
        # Later rounds: institutional/strategic heavy
        last = sched.rounds[-1]
        assert "strategic" in last.active_agent_types or "institutional" in last.active_agent_types
