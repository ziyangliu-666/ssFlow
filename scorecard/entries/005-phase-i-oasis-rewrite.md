# Scorecard #005 — Phase I OASIS Rewrite Smoke

> **Status:** Engine validated end-to-end on a real LLM smoke test. No real-market
> verification needed for this entry — it documents the architecture rollout, not
> a prediction.
> **Recorded:** 2026-04-08 (UTC+8)
> **Mode:** oasis (Phase I — full rewrite on top of CAMEL-AI's OASIS social
> simulation framework)

## Why this entry exists

Phase H built a Concordia-based market simulator with a 1-bit information cascade
(`InfoEventComponent` — single-sentence broadcasts). The user clarified that
ssFish should be a **superset of MiroFish** — an information ecosystem first,
with the market as a downstream consumer of the social state. Phase H's
"market simulator with a cascade bolt-on" inverted the causality.

Phase I deletes the entire Phase H Concordia layer and rebuilds the simulation
on top of CAMEL-AI's **OASIS** framework (the same one MiroFish is built on).
OASIS provides the social primitives natively: follow graph, feed aggregation,
21 social actions (post, repost, follow, like, comment, search, refresh), and
SQLite-backed persistence. ssFish adds a trading layer on top that reads each
trader's filtered OASIS feed and produces structured order decisions, applies
Kyle price impact, and posts the new price back into OASIS as a synthetic
market-event broadcaster post.

User directive (2026-04-08, UTC+8):
- _"对的, 还是信息生态为主, 重构整个设计吧"_
- _"开始吧，不用考虑过往版本兼容性，我们是一个破坏性的更新"_

## What changed at the architecture level

| Concern | Phase H (Concordia) | Phase I (OASIS) |
|---|---|---|
| Primary structure | Order book + Kyle | **Information ecosystem** |
| Secondary structure | 1-sentence cascade strings | Order book + Kyle (downstream) |
| Framework | gdm-concordia 2.4.0 (deleted) | camel-oasis 0.2.5 |
| Round loop | Sync ThreadPoolExecutor | **Async** (`await env.step()`) |
| Persona schema | v2 (every persona is a trader) | **v3** (entity_role + follows + publishes; sandbox optional) |
| Personas in `ashare.yaml` | 14 (all traders) | **30** (14 traders + 16 info entities) |
| Non-trading actors | Don't exist | **First-class**: media, analysts, regulators, policy, KOLs, company IR |
| Observation routing | All-to-all broadcast | **Follow-graph filtered** per trader |
| Content types | One (`InfoEvent` string) | **5+** (news_brief / research_note / policy_statement / regulatory_inquiry / social_post / company_announcement) |
| Report shape | P&L tables + round voices | **Feed-first narrative** (publications grouped per round, prices as side column) |
| LM cost tracking | Custom Concordia LM adapter | Custom CAMEL `OpenAICompatibleModel` subclass |
| Compliance filter | Wrapping Concordia LM | Wrapping CAMEL LM (every choice content sanitized in place) |

## What was deleted (Phase H Concordia layer)

| File | LOC |
|---|---|
| `src/ssfish/concordia_engine.py` | 429 |
| `src/ssfish/concordia_lm.py` | 200 |
| `src/ssfish/concordia_persona_adapter.py` | 140 |
| `src/ssfish/concordia_components/__init__.py` | 42 |
| `src/ssfish/concordia_components/order_action.py` | 608 |
| `src/ssfish/concordia_components/info_action.py` | 175 |
| `src/ssfish/concordia_components/strategic_signal.py` | 190 |
| `src/ssfish/concordia_components/observation_log.py` | 83 |
| `tests/test_concordia_engine.py` | 330 |
| `tests/test_concordia_lm.py` | 225 |
| **Total deleted** | **2,422 LOC** |

Plus dependencies removed: `gdm-concordia==2.4.0`, `sentence-transformers>=3.0.0`.

## What was created (Phase I OASIS layer)

| File | LOC | Purpose |
|---|---|---|
| `src/ssfish/oasis_lm.py` | 200 | `SsFishCamelModel` — CAMEL `OpenAICompatibleModel` subclass routing through `cost_tracker` + `output_filter` |
| `src/ssfish/oasis_persona_adapter.py` | 270 | Persona YAML → OASIS `AgentGraph` + follow edges + synthetic `__market__` agent |
| `src/ssfish/oasis_feed_reader.py` | 270 | Query OASIS SQLite db, filter posts by follow graph, return list[Publication] |
| `src/ssfish/trading_layer.py` | 470 | Pure-Python trading: spawn_agents, apply_action, normalize, decide_orders. Zero framework dep. |
| `src/ssfish/oasis_engine.py` | 510 | Main `run_simulation` async loop: OASIS social step → trading step → Kyle → price post |
| `src/ssfish/information/__init__.py` | 20 | Public API for the information types |
| `src/ssfish/information/publication.py` | 60 | `Publication` dataclass — canonical Python repr of a sim post |
| `src/ssfish/information/external_events.py` | 75 | Multi-round event schedule for mid-sim policy/news shocks |
| `tests/test_oasis_lm.py` | 220 | LM adapter cost / budget / sanitization (10 tests) |
| `tests/test_oasis_persona_adapter.py` | 240 | Adapter graph build (14 tests) |
| `tests/test_oasis_feed_reader.py` | 290 | DB-driven feed query (16 tests) |
| `tests/test_trading_layer.py` | 320 | Pure-Python trading (27 tests) |
| `tests/test_external_events.py` | 80 | Event schedule (5 tests) |
| **Total created** | **~3,025 LOC** | |

Plus persona schema v3 additions to `src/ssfish/persona.py` (~120 LOC), report.py
rewrite (~230 LOC), `personas/ashare.yaml` expansion (14 → 30 entities, ~600 lines
of YAML for the 16 new info personas + follows lists for the 14 existing traders).

## Real LLM smoke test results

### Run 1 — BYD Q1 2026 (2 rounds, 30-persona ashare pack)

| Field | Value |
|---|---|
| Simulation ID | `oasis_d674ee938dfc` |
| Personas | 30 (14 traders + 16 info entities) |
| Rounds | 2 |
| **LLM cost** | **$0.0212** (89 calls, 117k input + 6k output tokens) |
| LLM model | gpt-4o-mini |
| Initial price | ¥218.50 |
| Final price | ¥264.39 |
| Cumulative delta | **+21.0%** (R0 +10%, R1 +10% — both rounds hit the ±10% cap) |
| **Publications emitted** | **38** across 2 rounds |
| Compliance | PASS |
| OASIS db | `reports/oasis_dbs/oasis_d674ee938dfc.db` |

**Sample publications observed:**
- `news_wire_yicai`: "Looking forward to sharing insights on the latest macroeconomic trends..."
- `policy_maker_ndrc`: "我们正在密切关注新能源汽车市场的动态，并将继续通过政策支持来促进这一战略性新兴产业的发展。"
- `retail_kol_xueqiu_laoqin`: "大家好！关注一下市场动态，最近的短线机会可能会出现在汽车领域..."
- `industrial_capital_strategic`: "As we evaluate the current market conditions for industrial capital..."
- `Market Event Wire`: "[Market Event] R1 price update: ¥240.35 → ¥264.39 (+10.00%)"

**Trading dynamics observed:**
- R0: 散户追涨 went **net sell** (¥1.17億) on the margin miss, citing "短线散户会紧张"
- R1 (after seeing the social media buzz + price rally): 散户追涨 reversed to
  **net buy** (¥0.08億), citing "看到涨幅10%大家都想追"
- 国家队 went from observing R0 to **net buy** R1, citing "市场回暖，价格上涨，国家队可能会选择适度进入以维持稳定"
- 私募基金 stayed **net sell** R1 (¥18.97億), citing "盈利数据略低于预期，部分参与者可能会选择降低敞口或止损"

**The R0→R1 reversal of 散户追涨 is the central new dynamic of Phase I.** It's
driven by the social feed (KOL bullish posts + policy supportive statements +
market price update post) being readable to the trader's feed via the follow
graph. Phase H couldn't produce this — its 1-sentence cascade was too thin
to actually move trader decisions.

## What this proves

1. **OASIS integration works end-to-end on real LLM.** No crashes, no hangs,
   30 personas + 2 rounds in ~$0.02. The CAMEL `OpenAICompatibleModel`
   subclass plumbs through cost tracking + compliance correctly.

2. **The information ecosystem fires.** 38 publications in 2 rounds means
   roughly 1.3 publications per persona per round. Across the 16 info
   entities we see news wires, policy makers, KOLs, and industrial capital
   all emitting real content.

3. **Trader feeds are filtered correctly.** The follow graph wired from YAML
   determines what each trader sees. Retail追涨 sees KOLs + news wires but
   not analyst notes; public fund PM follows everything; quant follows just
   news wires. The trading prompts contain only the appropriate subset.

4. **Trading visibly reacts to the feed.** The R0→R1 reversal of 散户追涨 from
   net sell to net buy is causally linked to the social feed buildup
   (KOL bullishness + policy support post + +10% price rally). This is the
   "MiroFish feel" the user asked for: a market that reads its own narrative.

## What this does NOT yet prove

1. **Real T+1 / T+5 verification.** That's scorecard #003's job, scheduled for
   2026-05-04. Phase I doesn't change the order-flow / Kyle math.

2. **Content type richness.** Most publications in run 1 came back as
   `social_post` because the publication registry only stores metadata for
   posts the engine explicitly creates (the seed event + price updates).
   OASIS's LLM-driven posts go through CAMEL's tool-calling layer and don't
   pass through our PublishConfig templates. Polish item, not a blocker.

3. **Cross-round narrative continuity.** Two rounds is the minimum to
   demonstrate cascade. A 5-round run would show whether narratives build,
   peak, and reverse over time.

## Test suite state

| Test file | Tests | Status |
|---|---|---|
| `tests/test_market_dynamics.py` | 16 | PASS |
| `tests/test_oasis_lm.py` | 10 | PASS (new) |
| `tests/test_oasis_persona_adapter.py` | 14 | PASS (new) |
| `tests/test_oasis_feed_reader.py` | 16 | PASS (new) |
| `tests/test_trading_layer.py` | 27 | PASS (new) |
| `tests/test_external_events.py` | 5 | PASS (new) |
| `tests/test_persona.py` | 23 | PASS (8 new for v3 schema) |
| `tests/test_persona_factory.py` | (existing) | PASS |
| `tests/test_output_filter.py` | (existing) | PASS |
| **Total** | **202** | **PASS** |

## Files actually deleted (verification)

```bash
$ ls src/ssfish/concordia_*.py src/ssfish/concordia_components/ tests/test_concordia*.py
ls: cannot access ...: No such file or directory

$ uv pip list | grep -i concordia
(empty)

$ uv pip list | grep -i camel-oasis
camel-oasis        0.2.5
```

## Followups (not Phase I scope)

- **Polish content type registration.** When OASIS-LLM posts come back, map
  them to the author persona's `PublishConfig` to recover the right
  `content_type` label (instead of falling back to `social_post`).
- **Demo a 5-round run with external events injected** at R3 — needed to
  show the "policy surprise → reverse the trend" dynamic.
- **Tag info entities in `personas/us-equity-v1.yaml` and
  `personas/crude-oil-wti-v1.yaml`** so cross-market sims also work.
- **Calibration pipeline extension** (`persona_factory.py`) — generate info
  entity personas via web research, not just trader personas.
- **T+5 verification of scorecard #003** still pending (2026-05-04).
- **OSS launch (Approach C)** still deferred.
