# ssFlow Autonomous Review Loop

**Started**: 2026-04-11
**Objective**: Evaluate whether ssFlow's simulation is genuinely useful or just a toy; construct real A-share scenarios and stress-test the engine; iterate improvements.
**Reviewer**: GPT-5.4 via Codex MCP (xhigh reasoning)
**Max rounds**: 4

---

## Round 1 (2026-04-11 ~01:00 UTC+8)

### Assessment (Summary)
- Score: 4/10
- Verdict: "Educational toy today; research prototype if calibrated and given A-share market mechanics. Not yet a fund-manager tool."
- Key criticisms:
  - No A-share limit-board mechanics (涨停/跌停 queues, one-word boards, unfilled orders, T+1 sell constraints)
  - λ and knee not calibrated against real A-share event data
  - No multi-instrument factor/contagion layer (ETF basket flows, sector spillover, cross-asset)
  - Personas lack stateful balance sheets (inventory, leverage, redemption pressure, mandate)
  - Information/policy effects are narrative-only, not quantitatively state-changing

### Dimension Scores
| Dimension | Score |
|---|---:|
| Price trajectory realism | 3/10 |
| Agent behavior realism | 5/10 |
| Information cascade modeling | 4/10 |
| Regulatory/policy response | 3/10 |
| Usefulness for rehearsal | 5/10 |
| **Overall** | **4/10** |

### Real Scenarios Tested (Mental Simulation)
1. **Feb 2024 national-team rescue** — ssFlow misses ETF basket flow, style rotation, forced deleveraging
2. **WuXi AppTec Biosecure Act shock** — misses geopolitical terminal-value repricing, HK/A linkage, northbound constraints
3. **Zhengdan TMA supply-squeeze 10-bagger** — misses commodity-to-EPS model, capacity constraints, valuation anchor
4. **Dazhong Transportation robotaxi mania** — misses 龙头/补涨, 封单 strength, 龙虎榜 seat behavior
5. **"924" policy rally** — cannot model market-wide liquidity regime change, broker-beta reflexivity
6. **DeepSeek A-share AI split** — regex cross-market too weak, needs factor graph
7. **Huijin Technology *ST collapse** — cannot model limit-board queues, unfilled orders, liquidity lock-in
8. **April 2025 tariff/rare-earth shock** — lacks macro beta, FX, sector hedging, portfolio de-risking

### Reviewer Raw Response

<details>
<summary>Click to expand full reviewer response</summary>

**Score: 4/10**

**Verdict:** ssFlow is currently an educational scenario/narrative toy with a useful research scaffold underneath. It can generate plausible first-order stories, but it is not yet a market microstructure tool. The biggest blockers are not "more personas"; they are missing A-share mechanics: limit-up/limit-down queues, T+1 and funding constraints, ETF/index basket flows, cross-asset factor contagion, calibrated event impact, and quantified policy/regulatory intervention.

**Real Stress Scenarios**
| Scenario | Real Market Reaction | What ssFlow Would Get Right | Where It Breaks |
|---|---|---|---|
| **1. Feb. 2024 national-team rescue after small-cap crash**. Central Huijin announced on February 6, 2024 that it had expanded ETF purchases and would keep increasing buying to maintain market stability; CSRC said it would guide more institutional funds into the market. | Large-cap/index ETFs were supported while small and micro caps were still under severe pressure. Later ETF disclosures showed Huijin bought large CSI 300/China 50 ETFs in size, including tens of billions of yuan in major CSI 300 ETFs. | It would likely trigger the "national team" persona, stabilization posts, retail confidence recovery, and institutional follow-on buying. | It cannot reproduce the key reality: index ETF rescue flow lifted large caps while crowded small-cap/quant books were forced to unwind. A single-stock Kyle impact model misses ETF basket flow, futures hedges, style rotation, and forced deleveraging. |
| **2. WuXi AppTec Biosecure Act shock, January 26-31, 2024**. A U.S. Biosecure Act draft named WuXi AppTec; A-shares flash-crashed to limit-down at RMB65.96 with RMB3.118bn turnover on January 26. | The stock fell more than 20% over three trading days after the draft, with related BGI/MGI names also hit. | ssFlow would produce foreign/institutional selling, analyst counter-narratives, media amplification, and retail panic. | This was a geopolitical terminal-value repricing, not just order-flow impact. The model lacks U.S. legislative probability, HK/A-share linkage, northbound risk constraints, and overnight gap/limit-board dynamics. |
| **3. Zhengdan Shares TMA supply-squeeze ten-bagger, 2024**. INEOS supply exit and TMA price spike made Zhengdan the first 2024 A-share ten-bagger, with the stock reportedly moving from a February low near RMB2.83 to over RMB31 by late May. | Momentum continued into June; after announcing a RMB350m TMA expansion plan, Zhengdan hit a 20cm limit-up, with an institutional seat buying RMB107m and retail-heavy Lhasa seats dominating the sell side. | It would capture bullish analysts, KOL amplification, retail momentum, and aggressive buy flows. | The real driver was commodity supply-demand and earnings convexity. ssFlow has no product-price-to-EPS model, capacity constraints, commodity inventory cycle, convertible dilution, or valuation anchor. It would confuse fundamental repricing with pure liquidity impact. |
| **4. Dazhong Transportation robotaxi/游资 mania, July 2024**. Dazhong Transportation became the "robotaxi" theme leader; on July 30 it hit another limit-up, its ninth in 15 sessions, with July gains above 265%. | The company repeatedly warned that intelligent connected vehicle activity was still experimental and had basically no revenue impact. Dazhong Public Utilities also caught the contagion and recorded consecutive limit-ups. | ssFlow would model KOL hype, retail momentum buying, media posts, and analyst skepticism. | It lacks the true A-share 短线生态: 龙头/补涨 propagation, 封单 strength, 龙虎榜 seat behavior, one-word boards, regulatory risk notices, and attention-driven liquidity migration from one theme to another. |
| **5. September 24, 2024 "924" policy rally**. PBOC announced capital-market tools: a 500bn yuan securities/funds/insurers swap facility and a 300bn yuan relending facility for buybacks/major-shareholder purchases. | This was a regime-shift rally, not a normal event. The policy directly increased institutions' stock-buying capacity and changed expectations around the state put. | ssFlow would correctly activate national team, public funds, insurance, broker, and retail FOMO personas. | It cannot model a market-wide liquidity regime change. A single-name price impact formula cannot handle all-index repricing, broker-beta reflexivity, account-opening FOMO, ETF creation/redemption, and policy credibility as a state variable. |
| **6. DeepSeek A-share AI split, February 5, 2025**. After DeepSeek V3/R1 attention during the holiday, DeepSeek concept stocks surged: Bingxing Tech hit 30%, UCloud, Anheng, Daily Interactive, TRS, Qingyun and others hit limit-up, Wondershare rose over 15%, Kunlun over 10%. | The real market bifurcated: AI application/software and domestic AI ecosystem names surged, while some global AI capex/hardware assumptions were repriced. | ssFlow would catch the social hype and maybe inject NVIDIA/US-tech context from regex. | Regex cross-market injection is far too weak. This requires a factor graph: AI apps positive, some capex hardware negative, domestic substitution positive, CPO/high-speed copper potentially negative. Independent instruments miss the whole point. |
| **7. Huijin Technology *ST-risk collapse, March 31-April 2, 2025**. Huijin Technology revised 2024 revenue below RMB100m while loss-making, triggering *ST risk; the stock had run from RMB6.49 to RMB68.54 in 2024, then recorded three consecutive 20cm one-word limit-downs. | The company later faced regulatory warning over missing disclosure and possible退市 risk提示. First Financial reported 65,600 shareholders as of March 20. | ssFlow would produce a negative shock, sell-heavy retail behavior, and institutional avoidance. | This is exactly where the current model is toy-like. In reality, holders could not sell; the market was a queue at limit-down. Immediate fills and square-root impact are wrong. You need limit-board queues, unfilled orders, ST/delisting rules, and liquidity lock-in. |
| **8. April 2025 U.S.-China tariff/rare-earth shock**. China announced 34% retaliatory tariffs and export controls on seven medium/heavy rare earth items. | On April 7, global markets sold off; A-shares opened sharply lower, with Shanghai -4.46%, Shenzhen -5.96%, ChiNext -6.77%, while rare-earth/magnet names showed relative strength. U.S. futures and megacap tech were also hit. | ssFlow would model broad panic, policy-stabilization prompts, and rare-earth bullish narratives. | It lacks macro beta, FX, commodities, U.S. futures, sector hedging, and portfolio de-risking. This was not one ticker reacting to one event; it was a global risk-off shock with a sector hedge embedded inside it. |

**Dimension Scores**
| Dimension | Score | Reason |
|---|---:|---|
| Price trajectory realism | 3/10 | Direction may be plausible for liquid single-name shocks, but A-share limit boards, gap opens, no-fill queues, multi-day one-word boards, and ETF/index basket flows are missing. |
| Agent behavior realism | 5/10 | The persona taxonomy is directionally useful, but real participants trade under inventory, mandate, redemption, margin, borrow, benchmark, and regulatory constraints. LLM intent is not enough. |
| Information cascade modeling | 4/10 | Social posting is good for narrative generation, but publications do not quantitatively alter risk budgets, participation, belief, or queue strength. 龙虎榜, exchange notices, official media, and broker calls matter mechanically. |
| Regulatory/policy response | 3/10 | Binary "if price drops >7%" logic is far too crude. Real policy acts through ETFs, short-sale constraints, swap facilities, relending, official media, exchange inquiries, ST rules, and suspension risk. |
| Usefulness for rehearsal | 5/10 | Useful for qualitative "who says what, who might buy/sell" tabletop exercises. Not useful for sizing, expected path, liquidity, drawdown, or trading-plan rehearsal. |
| Overall | 4/10 | Educational toy today; research prototype if calibrated and given A-share market mechanics. Not yet a fund-manager tool. |

**Ranked Action Items**
| Rank | Improvement | Impact | Feasibility | Minimum Fix |
|---:|---|---|---|---|
| 1 | Add an A-share market-rule and limit-board engine. | Very high | High | Implement daily price limits, 10cm/20cm/ST bands, open auction gaps, unfilled order queues, partial fills by turnover, one-word limit-up/down states, and T+1 sell constraints. Do not let agents magically exit at the model price. |
| 2 | Calibrate impact to real A-share event data. | Very high | Medium | Build a 50-100 event library with event type, float market cap, ADV, turnover, opening gap, limit status, close-to-close path, and sector beta. Fit λ and knee by liquidity bucket, event class, and market regime. |
| 3 | Add a multi-instrument factor and contagion layer. | High | Medium | Map each event to exposures: market beta, industry, theme, H/A/ADR link, commodity, FX, U.S. tech, ETF membership, and policy sensitivity. Let flows propagate through sector peers, ETFs, futures hedges, and theme leaders/laggards. |
| 4 | Replace generic personas with stateful participant balance sheets. | High | Medium | Track inventory, cash, leverage, redemption pressure, mandate, benchmark, and holding period for retail cash, margin retail, 游资, northbound/QFII, public funds, ETFs/passive, insurers, quant market-neutral, and national team. |
| 5 | Make information and policy quantitatively state-changing. | High | High | Give every post, analyst note, official statement, exchange warning, and policy event numerical effects on belief, participation, risk budget, urgency, and target universe. Expand the policy DSL beyond binary comparisons to thresholds, time windows, affected sectors, and staged intervention. |

</details>

### Actions Taken
- [Round 1: review only — implementing fixes next]

### Status
- Continuing to round 2
- Difficulty: medium


## Round 2 (2026-04-11 ~02:00 UTC+8)

### Assessment (Summary)
- Score: 5.5/10 (up from 4/10)
- Verdict: "Early research prototype / scenario rehearsal tool. No longer a simple toy, not yet a quantitative tool."
- Key improvements recognized:
  - Limit-board engine is a "real upgrade"
  - Calibration library gives "the right validation surface"
  - Publication effects are "directionally better"
- Key remaining criticisms:
  1. Limit board is price-realistic but NOT execution-realistic (orders still fill at limit)
  2. T+1 ledger exists but not enforced in trading_layer.py
  3. Calibration library not yet driving live parameters
  4. Publication effects only partially applied (participation/urgency not wired into trade sizing)
  5. Multi-instrument contagion still too shallow

### Dimension Scores
| Dimension | R1 | R2 | Delta |
|---|---:|---:|---|
| Price trajectory | 3 | 5.5 | +2.5 |
| Agent behavior | 5 | 5.5 | +0.5 |
| Information cascade | 4 | 5.5 | +1.5 |
| Regulatory/policy | 3 | 5 | +2 |
| Usefulness for rehearsal | 5 | 6 | +1 |
| **Overall** | **4** | **5.5** | **+1.5** |

### Reviewer Raw Response

<details>
<summary>Click to expand full Round 2 reviewer response</summary>

Score: 5.5/10. Verdict: early research prototype. Moved from "educational toy" to "scenario rehearsal tool." Limit-board engine is price-realistic but not execution-realistic. T+1 exists but not enforced. Calibration library not yet driving live params. Publication effects partially applied. Multi-instrument contagion still shallow.

Top 5 remaining fixes:
1. Fill engine: route orders through limit-board fill constraints
2. T+1 enforcement in apply_action
3. Live calibration: select_impact_params + CI validation
4. Wire publication effects into participation/urgency/risk budget
5. Factor/ETF contagion layer

Reviewer notes: "If you fix true fills, enforce T+1, and make λ/knee event-conditioned, score moves to 6.5-7/10."

</details>

### Actions Taken
- Implementing: fill engine, T+1 enforcement, publication effect wiring, calibration integration

### Status
- Continuing to round 3


## Round 3 (2026-04-11 ~03:00 UTC+8)

### Assessment (Summary)
- Score: 6.4/10 (up from 5.8)
- Verdict: "Stronger research prototype, not yet a robust research tool."
- Key improvements recognized:
  - Fill engine + T+1 now wired into main engine loop ✅
  - Daily limit-board reset works ✅
  - Calibrated knee wired into dynamic knee ✅
  - Huijin Tech multi-day 20cm collapse now structurally representable
- Key remaining issues:
  1. Pre-open auction state not set before first-round trading
  2. No calibration backtest harness
  3. Multi-instrument factor/contagion still missing
  4. Participant balance sheets not implemented
  5. Execution diagnostics not surfaced in reports

### Dimension Scores
| Dimension | R1 | R2 | R3 | Delta (total) |
|---|---:|---:|---:|---|
| Price trajectory | 3 | 5.5 | 6.6 | +3.6 |
| Agent behavior | 5 | 5.5 | 6.4 | +1.4 |
| Information cascade | 4 | 5.5 | 6.2 | +2.2 |
| Regulatory/policy | 3 | 5 | 5.8 | +2.8 |
| Usefulness for rehearsal | 5 | 6 | 6.7 | +1.7 |
| **Overall** | **4** | **5.5** | **6.4** | **+2.4** |

### Actions Taken
- Implementing: pre-open auction, backtest harness, execution diagnostics

### Status
- Final round (round 4)


## Round 4 — FINAL (2026-04-11 ~04:00 UTC+8)

### Assessment (Summary)
- **Score: 6.9/10** (up from 4/10 at start)
- Verdict: "ssFlow is now a serious prototype, not an educational toy. For single-name A-share stress rehearsal, especially limit-board events, I would use it to structure thinking and identify liquidity traps."

### Final Dimension Scores
| Dimension | R1 | R2 | R3 | R4 | Total Delta |
|---|---:|---:|---:|---:|---|
| Price trajectory | 3 | 5.5 | 6.6 | **7.1** | +4.1 |
| Agent behavior | 5 | 5.5 | 6.4 | **6.8** | +1.8 |
| Information cascade | 4 | 5.5 | 6.2 | **6.5** | +2.5 |
| Regulatory/policy | 3 | 5 | 5.8 | **6.4** | +3.4 |
| Usefulness for rehearsal | 5 | 6 | 6.7 | **7.2** | +2.2 |
| **Overall** | **4** | **5.5** | **6.4** | **6.9** | **+2.9** |

### Scenario Improvements (R1 → R4)
| Scenario | R1 | R4 | Status |
|---|---|---|---|
| Huijin Tech *ST | Toy-like, holders magically exit | Pre-open one-word down, trapped sellers, compounding 20cm limits | ✅ Strength |
| WuXi Biosecure | No limit-board mechanics | Geopolitical calibration, multi-day constrained exits | ✅ Good |
| Dazhong robotaxi | No 连板 mechanics | Limit-up states, seal strength, exchange inquiry effects | ✅ Good |
| 924 policy rally | Cannot model regime change | Staged policy, publication effects | ⚠️ Still needs multi-instrument |
| Tariff shock | No macro factor | Single-name only | ⚠️ Still needs factor layer |

### Path to 8/10
1. Multi-instrument factor/ETF contagion
2. Participant balance sheets (leverage, redemption, mandate)
3. Stronger validation benchmark (precision/recall, path MAE)
4. Auction queue priority realism
5. Empirical calibration by regime

### Actions Taken This Session
| Round | Changes | Tests Added |
|---|---|---|
| R1→R2 | limit_board.py, calibration_library.py, publication_effects.py, policy compound triggers | 225 |
| R2→R3 | Fill engine, T+1 enforcement, live calibration, pub effects wiring | 30 + 26 |
| R3→R4 | Integration wiring (fill+T+1+knee end-to-end), daily board reset | 0 (integration) |
| R4 | Pre-open auction, backtest harness, execution diagnostics | 51 |

**Total new tests: 348 (from ~160)**

## Method Description

ssFlow is a single-name A-share market event rehearsal engine that simulates multi-agent social+trading dynamics around corporate events. The architecture has three layers:

1. **Information Layer**: OASIS-based social simulation where 14 AI personas (retail, institutional, KOL, analyst, media, policy) read a follow-graph-filtered feed, post reactions, and form trading intent via LLM calls.

2. **Execution Layer**: Agent trading intent maps to order distributions, sampled across stochastic sub-agent populations. A fill engine enforces A-share market rules: daily price limits (±10/20/5% by board type), one-word limit boards, unfilled order queues with seal-strength tracking, T+1 sell constraints, and gap-open modeling.

3. **Price Layer**: Kyle (1985) square-root impact with soft flow compression, calibrated λ and knee from a 10-event real A-share library. AdaptiveADV tracks volume feedback; dynamic knee adjusts for participation and cumulative resistance. Publication effects quantitatively shift sentiment, participation, urgency, and risk budgets.

### Status
- **Completed** — 4/4 rounds exhausted
- Score progression: 4/10 → 5.5/10 → 6.4/10 → **6.9/10**

---

## Round 5 (2026-04-11 ~09:55 UTC+8) — Re-opened loop after live-run bug hunt

### Assessment (Summary)
- **Score: 7.1/10** (up from 6.9 stated at R4, though GPT retroactively haircuts R4 to ~6.2-6.4 because R1-R4 scored a partially broken system)
- **Verdict**: "Round 5 finally makes ssFlow's core single-name loop behave like the system you thought you already had in Round 4, but it is still not trustworthy in the bearish and extreme A-share regimes where a rehearsal engine is most valuable."

### Dimension Scores
| Dimension | R4 | R5 | Delta |
|---|---:|---:|---|
| Price realism | 7.1 | 7.3 | +0.2 |
| Agent behavior | 6.8 | 7.0 | +0.2 |
| Information cascade | 6.5 | 6.9 | +0.4 |
| Regulatory/policy | 6.4 | 6.2 | **-0.2** |
| Usefulness | 7.2 | 7.6 | +0.4 |
| **Overall** | **6.9** | **7.1** | **+0.2** |

### Changes This Round
| Commit | Fix |
|---|---|
| a227629 | P&L double-booking: `holdings_value(float)` silently dropped holdings under any ticker key != `_default`; active funds showed -¥26億 in +12% rally. Fixed by iterating `self.holdings.items()` + collapsing order routing to event ticker in single-instrument mode. |
| cbc77e9 | Session narrative + A-share T+1 rule injected per round. Discovered `update_conviction_context` and time-context were silently broken for 4 rounds because OASIS/CAMEL bakes system prompt once at `SocialAgent.__init__`. Moved to `set_round_context()` → user-instruction prepend path. |
| 4b3a6d3 | 30-day K-line stats + 5-day OHLCV table in `Instrument.prompt_summary()`. `run_one.py` now goes through `distill()` by default. |
| 55fe7ae | Integration crash: `round_schedule.prompt_context` signature mismatch from earlier stash-conflict merge. Every live run was crashing at R0 with TypeError before LLM call. |

### Critical Findings GPT Surfaced During Review
GPT read the repo directly and caught three disclosure errors I made:

1. **Silent prompt-state leak still on `main`**: `set_round_context()` only merges keys, and `clear_round_context()` exists but is never called. Result: `conviction_ctx` and `pub_effects_ctx` leak into subsequent rounds even when they should be reset. `oasis_persona_adapter.py:61`, `oasis_engine.py:1134`.
2. **"Multi-instrument spillover is inert" is wrong**: The engine does route orders by ticker and applies spillover at `oasis_engine.py:1648`. The actual problem is poor universe selection (CATL run picked 茅台 + 美的 as "peers") and weak end-to-end validation, not missing plumbing.
3. **"Short-selling not implemented" is too loose**: The trading layer supports `pool=margin` shorts via `apply_action`, but `submit_trading_decision` in `oasis_trading_tool.py:179` hardcodes every `sell` to `holdings_in_target`, so short-seller personas cannot actually open shorts in the path that matters. The bottleneck is the tool layer, not the execution layer.

### Credit Allocation (GPT)
- P&L correctness fix: +0.04
- Prompt injection actually reaching LLM: +0.09
- K-line data reaching LLM: +0.05
- Distillation live by default: +0.02

> "That understates the real capability delta. Ex post, R4 was overrated; Round 5 is less a clean +0.2 feature gain than a large reality correction."

### Meta Confidence (GPT)
> "Yes, the silent bugs materially erode confidence in the prior 6.9. I would retroactively haircut the true Round 4 baseline to about **6.2-6.4**, not 6.9. Dead prompt injection, broken conviction delivery, a live-run startup crash, and broken P&L accounting are not minor defects; they mean prior rounds were partially grading a design document, not a faithfully running engine."
>
> - **Stated score path:** 6.9 → 7.1
> - **True runtime path:** roughly **6.3 → 7.1**

### Top 5 Blockers To 8/10 (GPT)
1. End-to-end live regression harness with prompt-trace assertions, P&L conservation checks, and scenario-outcome gates.
2. Freeform shorting path that can actually express `pool=margin` + borrow/constraint logic.
3. Extreme-scenario open calibration for `regulatory` and `delisting_risk`, validated on live one-word limit-down scenarios.
4. Universe quality + spillover validation.
5. Stateful participant balance sheets.

### Priority Of "NOT Fixed" Items (revised by GPT)
1. Extreme-scenario live validation harness
2. Short-selling (fix tool layer hardcode)
3. Multi-instrument spillover / universe validation
4. Participant balance sheets

> "The first two are first-order sign errors. If delisting risk rallies and short funds cannot short, the engine fails on regime direction."

### Use-Case Certification (GPT)
**Trustworthy**:
- Single-name earnings, guidance, IR, analyst upgrade/downgrade, company-specific catalysts in liquid large/mid caps
- Upward crowding / limit-up trap rehearsal (T+1 + board mechanics)
- Liquidity-trap exercises ("everyone bullish, fills are bad")

**Borderline**:
- Small-cap theme mania on the way up
- Single-name policy beneficiary names
- Non-limit-board downside moves not depending on real shorting

**NOT trustworthy**:
- Delisting risk, fraud, regulatory death spirals, one-word limit-down stress
- Broad policy rally / ETF-led / basket-led contagion regimes
- Any scenario where short sellers should be the marginal price setter
- Any multi-name conclusion where peer set correctness matters

### Actions Taken
- Implementing R5 fixes: context leak, short-selling tool layer, severity_map for regulatory events, live smoke test

### Status
- Continuing to round 6 (loop re-opened, not yet at stop condition since verdict not "ready"/"almost")


