"""Tests for Instrument and InstrumentUniverse."""

from ssflow.instrument import Instrument, InstrumentUniverse


def _make_universe():
    instruments = [
        Instrument(
            ticker="300750", name="宁德时代", market="ashare",
            relationship="event_subject", current_price=390.0, adv_value=8e9,
        ),
        Instrument(
            ticker="002594", name="比亚迪", market="ashare",
            relationship="customer", current_price=218.0, adv_value=5e9,
        ),
        Instrument(
            ticker="300014", name="亿纬锂能", market="ashare",
            relationship="competitor", current_price=45.0, adv_value=2e9,
        ),
        Instrument(
            ticker="159755", name="锂电池ETF", market="ashare",
            relationship="sector_etf", current_price=1.05, adv_value=1e8,
        ),
    ]
    return InstrumentUniverse(instruments=instruments, topic="宁德时代海外订单超预期")


class TestInstrument:
    def test_to_serializable(self):
        inst = Instrument(
            ticker="300750", name="宁德时代", market="ashare",
            relationship="event_subject", current_price=390.0, adv_value=8e9,
        )
        s = inst.to_serializable()
        assert s["ticker"] == "300750"
        assert s["current_price"] == 390.0


class TestInstrumentUniverse:
    def test_all_instruments(self):
        u = _make_universe()
        assert len(u.all_instruments) == 4

    def test_tickers(self):
        u = _make_universe()
        assert u.tickers == ["300750", "002594", "300014", "159755"]

    def test_event_subject_ticker(self):
        u = _make_universe()
        assert u.event_subject_ticker == "300750"

    def test_primary_ticker_compat(self):
        """primary_ticker is a deprecated alias for event_subject_ticker."""
        u = _make_universe()
        assert u.primary_ticker == u.event_subject_ticker

    def test_get_existing(self):
        u = _make_universe()
        assert u.get("002594").name == "比亚迪"

    def test_get_missing(self):
        u = _make_universe()
        assert u.get("999999") is None

    def test_prices(self):
        u = _make_universe()
        p = u.prices()
        assert p["300750"] == 390.0
        assert p["002594"] == 218.0

    def test_adv_values(self):
        u = _make_universe()
        a = u.adv_values()
        assert a["300750"] == 8e9

    def test_serializable_roundtrip(self):
        """New flat format round-trips correctly."""
        u = _make_universe()
        s = u.to_serializable()
        assert "instruments" in s
        assert "primary" not in s
        u2 = InstrumentUniverse.from_serializable(s)
        assert u2.event_subject_ticker == "300750"
        assert len(u2.instruments) == 4
        assert u2.topic == "宁德时代海外订单超预期"
        assert u2.get("300014").name == "亿纬锂能"

    def test_legacy_format_deserialization(self):
        """Legacy {"primary": ..., "related": [...]} format is accepted."""
        legacy = {
            "topic": "test",
            "primary": {
                "ticker": "300750", "name": "宁德时代", "market": "ashare",
                "relationship": "primary", "current_price": 390.0, "adv_value": 8e9,
            },
            "related": [
                {
                    "ticker": "002594", "name": "比亚迪", "market": "ashare",
                    "relationship": "customer", "current_price": 218.0, "adv_value": 5e9,
                },
            ],
        }
        u = InstrumentUniverse.from_serializable(legacy)
        assert len(u.instruments) == 2
        # "primary" relationship is mapped to "event_subject"
        assert u.instruments[0].relationship == "event_subject"
        assert u.event_subject_ticker == "300750"
        assert u.instruments[1].relationship == "customer"

    def test_prompt_summary_no_primary_label(self):
        u = _make_universe()
        s = u.prompt_summary()
        assert "宁德时代" in s
        assert "比亚迪" in s
        assert "竞品" in s
        assert "板块ETF" in s
        # No "主体" label — instruments are presented equally
        assert "主体" not in s
        # Contains the equal-access language
        assert "任何一个" in s

    def test_compute_spillover_basic(self):
        """Instruments with no direct flow get spillover from those that do."""
        u = _make_universe()
        source_deltas = {"300750": 0.05}  # event subject moved +5%
        spill = u.compute_spillover(source_deltas)
        # 300750 is NOT in the result (it has direct flow)
        assert "300750" not in spill
        # Others should have spillover values
        assert "002594" in spill
        assert "300014" in spill
        assert "159755" in spill
        # Customer (比亚迪) should co-move (positive)
        assert spill["002594"] > 0
        # Competitor (亿纬锂能) should diverge (negative)
        assert spill["300014"] < 0

    def test_compute_spillover_multiple_sources(self):
        """Spillover works when multiple instruments have direct flow."""
        u = _make_universe()
        source_deltas = {"300750": 0.05, "002594": -0.02}
        spill = u.compute_spillover(source_deltas)
        assert "300750" not in spill
        assert "002594" not in spill
        assert "300014" in spill
        assert "159755" in spill
