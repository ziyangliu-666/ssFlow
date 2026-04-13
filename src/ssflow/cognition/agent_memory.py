"""Agent memory: per-agent persistent state across rounds.

Wraps the self_model subsystem and adds trading-specific state:
- Fill history (what was bought/sold, at what price, when)
- Pending orders (orders resting in the book)
- Prior expectations (what the agent expected before the event)
- Performance tracking (cumulative P&L, win/loss streak)

This state is injected into the LLM prompt so each agent's decision
is informed by their specific portfolio history, not just the class
average.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class FillRecord:
    """A single fill from the agent's perspective."""

    round_idx: int
    tick: int
    side: str           # "buy" or "sell"
    price: float
    quantity: float
    value: float        # price * quantity
    fill_id: str = ""


@dataclass
class AgentMemory:
    """Per-agent persistent state for the cognition pipeline.

    Created once per agent at spawn time. Updated each round after
    fills are processed. Used to build portfolio-aware LLM prompts.

    Usage:
        mem = AgentMemory(agent_id="a001", persona_id="retail_chaser")
        mem.record_fill(FillRecord(round_idx=0, tick=10, side="buy", price=45.5, quantity=100, value=4550))
        prompt = mem.to_portfolio_prompt(current_price=46.0)
    """

    agent_id: str
    persona_id: str

    # Portfolio state (mirrors TraderInstance but from agent's perspective)
    initial_cash: float = 0.0
    current_cash: float = 0.0
    holdings: dict[str, float] = field(default_factory=dict)  # {ticker: shares}
    avg_cost: dict[str, float] = field(default_factory=dict)  # {ticker: weighted avg buy price}

    # Fill history
    fill_history: list[FillRecord] = field(default_factory=list)
    pending_order_ids: list[str] = field(default_factory=list)

    # Performance metrics
    realized_pnl: float = 0.0
    n_trades: int = 0
    n_wins: int = 0               # trades that were profitable
    current_streak: int = 0       # positive = winning, negative = losing

    # Prior expectations (set before the event)
    prior_expectation: str = ""   # e.g., "expected revenue +20%"
    prior_price_target: float = 0.0

    def record_fill(self, fill: FillRecord) -> None:
        """Record a fill and update portfolio metrics."""
        self.fill_history.append(fill)
        self.n_trades += 1

        ticker = "_default"  # simplified for now

        if fill.side == "buy":
            # Update average cost
            prev_shares = self.holdings.get(ticker, 0.0)
            prev_cost = self.avg_cost.get(ticker, 0.0)
            new_shares = prev_shares + fill.quantity
            if new_shares > 0:
                self.avg_cost[ticker] = (
                    (prev_cost * prev_shares + fill.price * fill.quantity) / new_shares
                )
            self.holdings[ticker] = new_shares
            self.current_cash -= fill.value

        elif fill.side == "sell":
            # Compute realized P&L on the sold shares
            cost_basis = self.avg_cost.get(ticker, fill.price)
            pnl = (fill.price - cost_basis) * fill.quantity
            self.realized_pnl += pnl

            prev_shares = self.holdings.get(ticker, 0.0)
            self.holdings[ticker] = max(0.0, prev_shares - fill.quantity)
            self.current_cash += fill.value

            if pnl > 0:
                self.n_wins += 1
                self.current_streak = max(1, self.current_streak + 1)
            elif pnl < 0:
                self.current_streak = min(-1, self.current_streak - 1)

    def unrealized_pnl(self, current_prices: dict[str, float]) -> float:
        """Compute unrealized P&L across all holdings."""
        total = 0.0
        for ticker, shares in self.holdings.items():
            if shares > 0 and ticker in current_prices:
                cost = self.avg_cost.get(ticker, 0.0)
                total += (current_prices[ticker] - cost) * shares
        return total

    def total_pnl(self, current_prices: dict[str, float]) -> float:
        """Realized + unrealized P&L."""
        return self.realized_pnl + self.unrealized_pnl(current_prices)

    def nav(self, current_prices: dict[str, float]) -> float:
        """Current net asset value."""
        holdings_value = sum(
            shares * current_prices.get(ticker, 0.0)
            for ticker, shares in self.holdings.items()
        )
        return self.current_cash + holdings_value

    def to_portfolio_prompt(self, current_price: float, ticker: str = "_default") -> str:
        """Render portfolio state as a prompt section.

        This is injected into the LLM prompt so the agent's decision
        is portfolio-aware.
        """
        shares = self.holdings.get(ticker, 0.0)
        avg = self.avg_cost.get(ticker, 0.0)
        prices = {ticker: current_price}

        lines = [
            f"Cash: {self.current_cash:,.0f}",
            f"Holdings: {shares:,.0f} shares",
        ]

        if shares > 0 and avg > 0:
            unrealized = (current_price - avg) * shares
            pct = (current_price / avg - 1.0) * 100
            lines.append(f"Avg cost: {avg:.2f} (unrealized {pct:+.1f}%, {unrealized:+,.0f})")

        if self.realized_pnl != 0:
            lines.append(f"Realized P&L: {self.realized_pnl:+,.0f}")

        if self.n_trades > 0:
            win_rate = self.n_wins / self.n_trades * 100
            lines.append(f"Trades: {self.n_trades} ({win_rate:.0f}% win rate)")
            if abs(self.current_streak) >= 2:
                streak_word = "winning" if self.current_streak > 0 else "losing"
                lines.append(f"Streak: {abs(self.current_streak)} {streak_word}")

        if self.prior_expectation:
            lines.append(f"Prior expectation: {self.prior_expectation}")

        # Recent fills (last 3)
        recent = self.fill_history[-3:]
        if recent:
            lines.append("Recent trades:")
            for f in recent:
                lines.append(f"  R{f.round_idx}: {f.side} {f.quantity:.0f} @ {f.price:.2f}")

        return "\n".join(lines)

    def to_serializable(self) -> dict:
        """Serialize for event bus / reports."""
        return {
            "agent_id": self.agent_id,
            "persona_id": self.persona_id,
            "cash": self.current_cash,
            "holdings": dict(self.holdings),
            "avg_cost": dict(self.avg_cost),
            "realized_pnl": self.realized_pnl,
            "n_trades": self.n_trades,
            "n_wins": self.n_wins,
        }


__all__ = ["AgentMemory", "FillRecord"]
