"""Sandbox templates — base archetypes for dynamic entity graph generation.

Each template defines:
  - entity_archetypes: slots that the sandbox generator fills with real entities
  - default_flows: resource dependencies between archetype slots
  - default_thresholds: conditions + effects, expressed as compilable strings

Templates are market-specific. The generator picks a template based on the
event's market field, then the LLM tunes it per topic.

Design principle: 主角单独，配角合并。Templates define 3-5 protagonist slots
plus one "other" catch-all slot.
"""

from __future__ import annotations

from typing import Any


# ─────────────────────── A-share Equity ───────────────────────

ASHARE_EQUITY_TEMPLATE: dict[str, Any] = {
    "id": "ashare_equity",
    "display_name": "A股权益",
    "description": "A-share equity event (earnings, guidance, sector shock)",

    "entity_archetypes": [
        {
            "slot": "subject_company",
            "entity_type": "company",
            "display_name_hint": "事件主体公司",
            "default_state": {
                "inventory_months": 2.0,
                "cash_bn": 30.0,
                "capacity_utilization": 0.80,
                "gross_margin": 0.20,
                "order_backlog_months": 1.5,
                "stock_price": 0.0,  # filled from market data
                "price_change_pct": 0.0,
            },
            "default_labels": {
                "inventory_months": "库存(月)",
                "cash_bn": "现金(亿元)",
                "capacity_utilization": "产能利用率",
                "gross_margin": "毛利率",
                "order_backlog_months": "订单积压(月)",
                "stock_price": "股价",
                "price_change_pct": "涨跌幅(%)",
            },
            "persona_link": "company_ir",
        },
        {
            "slot": "key_supplier",
            "entity_type": "supplier",
            "display_name_hint": "关键供应商",
            "default_state": {
                "inventory_months": 1.5,
                "subject_revenue_share": 0.25,
                "production_capacity": 100.0,
            },
            "default_labels": {
                "inventory_months": "库存(月)",
                "subject_revenue_share": "主体营收占比",
                "production_capacity": "产能(万/月)",
            },
            "persona_link": None,
        },
        {
            "slot": "retail_class",
            "entity_type": "trader_class",
            "display_name_hint": "散户群体",
            "default_state": {
                "avg_position_pct": 0.40,
                "margin_utilization": 0.65,
                "sentiment_score": 0.0,
                # price_change_pct is written by entity_engine each round from
                # the cumulative price move. It must be declared here so the
                # threshold at line ~191 (retail FOMO at +15%) can compile
                # against this entity — without it the threshold is silently
                # dropped by sandbox_generator as "unknown state variable".
                "price_change_pct": 0.0,
            },
            "default_labels": {
                "avg_position_pct": "平均仓位(%)",
                "margin_utilization": "融资利用率",
                "sentiment_score": "情绪指数",
                "price_change_pct": "涨跌幅(%)",
            },
            "persona_link": "retail_short_term_chaser",
        },
        {
            "slot": "institutional_class",
            "entity_type": "trader_class",
            "display_name_hint": "机构投资者",
            "default_state": {
                "avg_position_pct": 0.25,
                "max_position_pct": 0.50,
                "research_coverage_count": 15,
                "price_change_pct": 0.0,
            },
            "default_labels": {
                "avg_position_pct": "平均仓位(%)",
                "max_position_pct": "仓位上限(%)",
                "research_coverage_count": "研报覆盖数",
                "price_change_pct": "涨跌幅(%)",
            },
            "persona_link": "mutual_fund_active_pm",
        },
        {
            "slot": "market_environment",
            "entity_type": "other",
            "display_name_hint": "市场环境(合并)",
            "default_state": {
                "sector_avg_pe": 25.0,
                "sector_30d_return_pct": 0.0,
                "northbound_flow_5d_bn": 0.0,
                "margin_balance_bn": 0.0,
                "index_level": 3000.0,
            },
            "default_labels": {
                "sector_avg_pe": "行业平均PE",
                "sector_30d_return_pct": "行业30日涨跌(%)",
                "northbound_flow_5d_bn": "北向5日净流入(亿)",
                "margin_balance_bn": "融资余额(亿)",
                "index_level": "指数点位",
            },
            "persona_link": None,
        },
    ],

    "default_flows": [
        {
            "source_slot": "key_supplier",
            "target_slot": "subject_company",
            "resource_type": "核心零部件",
            "rate_per_round": 0.3,
            "source_var": "inventory_months",
            "target_var": "inventory_months",
            "label": "供应链",
        },
    ],

    "default_thresholds": [
        # ── inject_event: narrative influence (soft) ──
        {
            "entity_slot": "subject_company",
            "condition": "inventory_months > 3.0",
            "description": "库存积压超过3个月 → 去库存压力",
            "effect_type": "inject_event",
            "event_text_template": "[预警] {display_name}库存积压超过3个月,面临去库存压力,可能影响未来利润率",
        },
        {
            "entity_slot": "subject_company",
            "condition": "price_change_pct < -8.0",
            "description": "日内跌超8% → 监管问询",
            "effect_type": "inject_event",
            "event_text_template": "[监管] 交易所向{display_name}发出关注函,要求说明股价异常波动原因",
        },
        # ── mutate_state: mechanical state change ──
        {
            "entity_slot": "subject_company",
            "condition": "cash_bn < 15.0",
            "description": "现金储备不足 → 削减资本开支",
            "effect_type": "mutate_state",
            "state_mutations": {"capacity_utilization": 0.60},
        },
        # ── force_action: hard trading constraints (the teeth) ──
        # Downside teeth (v1 + prior)
        {
            "entity_slot": "retail_class",
            "condition": "margin_utilization > 0.72",
            "description": "融资利用率逼近警戒线 → 券商强平卖出50%持仓",
            "effect_type": "force_action",
            "forced_side": "sell",
            "forced_quantity_pct": 0.50,
        },
        {
            "entity_slot": "institutional_class",
            "condition": "price_change_pct < -5.0",
            "description": "累计跌超5% → 机构合规风控减仓20%",
            "effect_type": "force_action",
            "forced_side": "sell",
            "forced_quantity_pct": 0.20,
        },
        # Upside teeth (v2, backtest_monthly_v1.md §3.5).
        # Without these, strong policy-driven rallies fizzle because the
        # sim has no structural mechanism pushing marginal capital INTO
        # the trade. Real 924-style events see institutions chasing the
        # breakout + retail FOMO as momentum compounds.
        #
        # Thresholds tightened 2026-04-12 after iter2 backtest showed
        # policy rallies rarely cross the +15%/+20% gates within the sim
        # horizon because the bear doom-loop caps cumulative upside
        # before the FOMO gates trigger. Lower gates (+5% retail,
        # +8% institutional) match real A-share behavior where
        # institutions start chasing at the first +5-10% breakout day.
        {
            "entity_slot": "institutional_class",
            "condition": "price_change_pct > 8.0",
            "description": "累计涨超8% → 机构动量跟随买入15%",
            "effect_type": "force_action",
            "forced_side": "buy",
            "forced_quantity_pct": 0.15,
        },
        {
            "entity_slot": "retail_class",
            "condition": "price_change_pct > 5.0",
            "description": "累计涨超5% → 散户FOMO追涨加仓20%",
            "effect_type": "force_action",
            "forced_side": "buy",
            "forced_quantity_pct": 0.20,
        },
    ],
}


# ─────────────────────── Template Registry ───────────────────────

TEMPLATES: dict[str, dict[str, Any]] = {
    "ashare": ASHARE_EQUITY_TEMPLATE,
    "ashare_equity": ASHARE_EQUITY_TEMPLATE,
}


def get_template(market: str) -> dict[str, Any]:
    """Look up a template by market slug. Falls back to ashare."""
    return TEMPLATES.get(market, ASHARE_EQUITY_TEMPLATE)


__all__ = [
    "ASHARE_EQUITY_TEMPLATE",
    "TEMPLATES",
    "get_template",
]
