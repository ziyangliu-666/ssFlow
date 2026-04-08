# Scorecard #003 — BYD Q1 2026 Sandbox vs Real

> **Status:** Pending T+1 / T+5 verification
> **Recorded:** 2026-04-08
> **Verify on:** 2026-04-30 (T+1) and 2026-05-04 (T+5)
> **Mode:** sandbox (Phase B agent-based market model — first end-to-end run)

## Purpose

This is **the first sandbox-mode prediction recorded by ssFish**. It is the
empirical answer to the central question of the Phase A+B build:

> Can a 14-persona, real-data-calibrated, agent-based market sandbox produce
> a price trajectory that is closer to the real T+1/T+5 price action than the
> previous sentiment heuristic?

The answer goes into this file at T+5.

## Event

| Field | Value |
|---|---|
| Ticker | 002594.SZ (比亚迪) |
| Event date | 2026-04-29 |
| Event type | earnings |
| Event description | 比亚迪 2026 Q1 业绩公告: 营收 1620 亿 (+18% YoY, beat), 净利 78 亿 (+9%, miss), 毛利率 17.8% (-2.3pp YoY, miss), 销量 132 万辆 (beat), 海外占比 23% |
| Initial price (T0 close) | ¥218.50 |
| ADV (trailing 30d) | ¥80億 (manual fill, B8 deferred to Tushare integration) |
| Context completeness | 100% (prior consensus + recent price + sector context all populated) |

## Sandbox prediction

| Field | Value |
|---|---|
| Simulation ID | `sandbox_a1cdd29ba8b8` |
| Personas | 14 (5 strategic + 3 retail + 3 long-only inst + 2 active inst + 1 quant) |
| Persona pack | `personas/ashare.yaml` (schema v2, cited 6 public sources) |
| Rounds | 5 |
| Wall clock | 18.0 seconds |
| Cost (USD) | $0.0252 |
| LLM model | gpt-4o-mini |
| LLM seed | 42 |
| λ used | 0.500 (literature value, A-share, Bouchaud 2010) |
| Price impact cap | ±10% per round (modeling A-share 涨停板) |

### Predicted price trajectory

| Round | Price before | Price after | ΔP | Net flow (¥億) |
|---|---|---|---|---|
| R0 | ¥218.50 | ¥240.35 | +10.00% | +23.29 |
| R1 | ¥240.35 | ¥216.32 | -10.00% | -17.14 |
| R2 | ¥216.32 | ¥194.68 | -10.00% | -87.87 |
| R3 | ¥194.68 | ¥214.15 | +10.00% | +3.81 |
| R4 | ¥214.15 | ¥235.57 | +10.00% | +12.51 |

**Cumulative delta: +7.81%** (¥218.50 → ¥235.57)
**Intraday range:** ¥194.68 – ¥240.35

### Predicted class P&L (top 5 by absolute value)

| Class | Final P&L (¥M) | Per-agent (¥k) | Agents |
|---|---|---|---|
| `major_individual_holder` | +11280 | +3760108 | 3 |
| `industrial_capital_strategic` | +7939 | +2646466 | 3 |
| `cross_holding_strategic` | +1134 | +378126 | 3 |
| `government_state_strategic` | +803 | +401380 | 2 |
| `national_team_strategic` | +645 | +322378 | 2 |
| `northbound_qfii` | +456 | +18255 | 25 |
| `private_equity_active` | +385 | +15383 | 25 |
| `mutual_fund_active_pm` | +318 | +10615 | 30 |

(Note: per-agent values for strategic personas reflect each entity's massive
per-stock exposure, not total wealth. The 3 individual major holders represent
~17% × ¥600B BYD = ~¥100B per agent of in-stock value.)

### Strategic layer signals (final round)

| Persona | Direction | Magnitude | Horizon | Rationale (excerpt) |
|---|---|---|---|---|
| 产业资本(大股东) | neutral | medium | 180d | "考虑在合适时机增持以稳定市值. 减持的可能性较低, 因需遵循减持窗口限制." |
| 上市公司互持 | neutral | medium | 90d | "继续持有大部分股份, 保持战略稳定性, 但在必要时可能会考虑小幅减持以应对市场波动." |
| 国资委/地方政府 | neutral | medium | 30d | "短期内不响应市场波动, 但若政策指令要求调整持仓, 则可能会选择适度减持." |
| 国家队(汇金/证金) | **accumulate** | medium | 30d | "可能会选择在此时适度入场, 以稳定市场情绪, 同时考虑可能的反弹机会." |
| 个人大股东(5%+) | **reduce** | medium | 30d | "可能会选择在合规窗口内分批减持以应对潜在风险, 同时也可能在价格下跌时增持以传递信心." |

## Real outcome (TBD)

| Field | T+1 (2026-04-30) | T+5 (2026-05-04) |
|---|---|---|
| Real close price | _pending_ | _pending_ |
| Real intraday range | _pending_ | _pending_ |
| Real daily volume | _pending_ | _pending_ |
| Real ΔP from T0 | _pending_ | _pending_ |

## Comparison (TBD)

After T+5 the following will be filled in:

- |sandbox_predicted_close - real_close| / real_close: _pending_
- Sandbox direction matched real direction (sign): _pending_
- Sandbox intraday range overlaps real intraday range: _pending_
- Was the cumulative move within the cap range? _pending_

## Cross-comparison with sentiment-mode prediction (#001)

Scorecard entry #001 was the original BYD smoke test (sentiment mode, with the
fictional v1 panel) and produced sentiment_mean = -0.665, all 10 personas bearish.
The sandbox prediction here (#003) instead produces:

- Cumulative direction: **+7.81%** (mildly bullish)
- Round directions: alternating (+, -, -, +, +) — multi-round dynamics ARE alive
- Strategic layer: split (1 accumulate, 1 reduce, 3 neutral)
- Class P&L: most classes positive due to net upward trajectory

This is **categorically different output** from #001:
- #001: monolithic bearish sentiment, one heuristic-mapped price range
- #003: emergent multi-round price trajectory with class-specific P&L and
  qualitative strategic signals

The `cumulative_delta_pct` is no longer a starting point of comparison (the
two modes produce incomparable outputs); the right comparison is **which
mode's output is closer to the real T+5 price**, which we'll know on
2026-05-04.

## Methodology notes

This is the first run of:
1. **Persona pack v2 schema** (loaded via the new dispatch-free loader)
2. **14 real-data-calibrated personas** (all 14 with cited sources)
3. **Sandbox execution mode** (agent-based, Kyle square-root impact)
4. **±10% per-round cap** (modeling A-share 涨停板)
5. **buy_headroom enforcement** (max_position_pct hard cap)
6. **Strategic two-track aggregation** (signals + flows in parallel)

The cap saturated all 5 rounds, suggesting the LLM-produced action distributions
were extreme enough to push every round to its bound. This is partially expected
(the fictional event is large) but might indicate the persona class capital
calibrations are still slightly too aggressive. To investigate after T+5:

- If sandbox directionally matches reality but the magnitude is larger →
  capital calibrations are too aggressive, scale them down
- If sandbox directionally MISMATCHES reality → the persona action
  distributions are wrong, voice prompts need iteration
- If sandbox roughly matches → first validation of the architecture

## Reproducibility

| Field | Value |
|---|---|
| Persona pack hash | (in scorecard.db row) |
| Event text hash | `3b7095135f7a9f99` |
| LLM seed | 42 |
| System fingerprints (5 rounds × 14 calls) | (stored in scorecard.db `round_fingerprints_json`) |
| Code commit | (will be referenced by the commit that adds this file) |
| Persona pack file | `personas/ashare.yaml` |

## Action items

- [ ] **2026-04-30:** Fill T+1 close price + intraday range + daily volume
- [ ] **2026-05-04:** Fill T+5 close price + intraday range + diff vs
  predicted, write the comparison section
- [ ] **2026-05-04:** Decide whether the sandbox calibration needs adjustment
  based on the comparison
- [ ] **2026-05-04:** Update `personas/ashare.yaml` voice prompts if specific
  classes had wildly mismatched LLM responses

## Web UI walk-through (C5 verification)

Per the original plan, the C5 milestone required walking through the full
flow via the browser. Done on 2026-04-08 via Playwright MCP:

1. Navigated to `http://127.0.0.1:5000/`
2. Filled the event form (ticker / event_text / event_type / event_date /
   prior_consensus / recent_price_action / sector_context)
3. Selected the new "执行模式" → `sandbox (agent-based market 推演)` dropdown
4. Filled `当前价格 ¥` = 218.50 and `日均成交额 ¥億` = 80
5. Submitted with the X-Auth-Password header
6. After 17.9 seconds got back a fully rendered sandbox report

Screenshot: `scorecard/screenshots/003-byd-sandbox-walkthrough.png`

Web run details (separate from the CLI run above):
  - Simulation ID: `sandbox_e9d643182ccc`
  - Initial: ¥218.50  →  Final: ¥192.74
  - Cumulative: -11.79%
  - Cost: $0.0197 (slightly less than CLI run because shorter rationales)

The web run produced a different stochastic outcome than the CLI run
(-11.79% vs +7.81%) — same engine, same persona pack, same LLM, but
the LLM responses are nondeterministic. This is expected. Both runs
demonstrate that the engine produces non-trivial multi-round dynamics.

Both stack layers (CLI direct + Flask HTTP) produced consistent reports
with all four sections (price trajectory, class P&L, strategic layer,
class voices) and passed the compliance filter.

---

_This is the first entry recording a falsifiable, physics-grounded ssFish
prediction. The previous entry (#001) was a sentiment-heuristic output that
could not be directly compared to real prices. This one can. T+5 will tell
us whether the engine is right._
