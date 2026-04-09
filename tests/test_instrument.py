"""Tests for Instrument and InstrumentUniverse."""

from ssflow.instrument import Instrument, InstrumentUniverse


def _make_universe():
    primary = Instrument(
        ticker="300750", name="宁德时代", market="ashare",
        relationship="primary", current_price=390.0, adv_value=8e9,
    )
    related = [
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
    return InstrumentUniverse(primary=primary, related=related, topic="宁德时代海外订单超预期")


class TestInstrument:
    def test_to_serializable(self):
        inst = Instrument(
            ticker="300750", name="宁德时代", market="ashare",
            relationship="primary", current_price=390.0, adv_value=8e9,
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

    def test_primary_ticker(self):
        u = _make_universe()
        assert u.primary_ticker == "300750"

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
        u = _make_universe()
        s = u.to_serializable()
        u2 = InstrumentUniverse.from_serializable(s)
        assert u2.primary_ticker == "300750"
        assert len(u2.related) == 3
        assert u2.topic == "宁德时代海外订单超预期"
        assert u2.get("300014").name == "亿纬锂能"

    def test_prompt_summary(self):
        u = _make_universe()
        s = u.prompt_summary()
        assert "宁德时代" in s
        assert "主体" in s
        assert "比亚迪" in s
        assert "竞品" in s
        assert "板块ETF" in s
