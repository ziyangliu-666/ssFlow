# Scorecard #004 — Phase H Concordia Rewrite Smoke

> **Status:** Engine validated, no real-market verification needed (this entry
> documents the rewrite, not a prediction).
> **Recorded:** 2026-04-08 (UTC+8)
> **Mode:** concordia (Phase H — full rewrite on top of gdm-concordia v2.4.0)

## Why this entry exists

This is the **first end-to-end run of the Concordia-based simulation engine**
that replaced the legacy `sandbox.py` orchestrator. Phase H deleted ~2,347 LOC
of hand-rolled orchestration and rebuilt the round loop on top of Google
DeepMind's Concordia generative-agent framework, with custom components for
order flow + cascade events + strategic signals.

The user directive (UTC+8 2026-04-08): _"不需要并存，你就全面接入就行，
现有的落后设计架构可以全部去除... 现在就是快速迭代期"_

This entry isn't measuring prediction quality — it's verifying the engine
runs end-to-end on real personas + real LLM calls + real cascade dynamics
without crashing.

## What changed at the architecture level

| Concern | Phase B (legacy) | Phase H (Concordia) |
|---|---|---|
| Round loop | `sandbox.run_sandbox_simulation` (asyncio.gather) | `concordia_engine.run_simulation` (sync, ThreadPoolExecutor) |
| Per-persona state | `Agent` dataclass + `spawn_agents` | Same dataclass, lifted into `concordia_components/order_action.py` |
| Action solicitation | `chat_action_distribution` (async) | `OrderActingComponent.get_action_attempt` calls `chat_json_sync` |
| Memory / observation | None (each round saw only price) | `ObservationLogComponent` + GM-broadcast `entity.observe()` |
| Cascade events | None (price-only feedback) | **`InfoEventComponent` — new** |
| Strategic signal | Embedded in `class_flow` | `StrategicSignalComponent` — separate ContextComponent |
| LLM cost tracking | `llm_client.cost_tracker` (async path) | Same singleton, plus `chat_sync` / `chat_json_sync` paths feed it |
| Compliance filter | `output_filter.assert_compliant` on final report | Same, plus `concordia_lm.SsFishLanguageModel` sanitizes every Concordia internal LM call |

## What was deleted

| File | LOC |
|---|---|
| `src/ssfish/sandbox.py` | 1141 |
| `tests/test_sandbox.py` | 1206 |
| **Total** | **2347** |

## What was created

| File | LOC | Purpose |
|---|---|---|
| `src/ssfish/market_dynamics.py` | ~100 | Lifted Kyle formula + λ literature table |
| `src/ssfish/concordia_lm.py` | ~200 | Custom Concordia `LanguageModel` routing through `cost_tracker` + `output_filter` |
| `src/ssfish/concordia_engine.py` | ~340 | Main `run_simulation` orchestrator |
| `src/ssfish/concordia_persona_adapter.py` | ~140 | Persona → EntityAgent bridge |
| `src/ssfish/concordia_components/order_action.py` | ~470 | Privileged ActingComponent (does the structured chat_json call) |
| `src/ssfish/concordia_components/info_action.py` | ~170 | Cascade event emitter |
| `src/ssfish/concordia_components/strategic_signal.py` | ~170 | Strategic-layer signal |
| `src/ssfish/concordia_components/observation_log.py` | ~95 | Bounded FIFO observation memory |
| `tests/test_concordia_engine.py` | ~330 | End-to-end with stubbed LM (12 tests) |
| `tests/test_concordia_lm.py` | ~225 | LM adapter cost / budget / compliance (11 tests) |
| `tests/test_market_dynamics.py` | ~110 | Kyle formula tests (16 tests, lifted from test_sandbox.py) |

Plus: 1 new optional persona field `emits_info_events: bool` (and 2 personas
in `personas/ashare.yaml` tagged with it for the smoke test).

## Smoke test runs

### Run 1 — BYD Q1 2026 (3 rounds, A-share)

| Field | Value |
|---|---|
| Simulation ID | `concordia_85c47504ce04` |
| Personas | 14 (`personas/ashare.yaml`) |
| Rounds | 3 |
| Wall clock | ~30 seconds |
| Cost (USD) | $0.0127 |
| LLM model | gpt-4o-mini |
| Initial price | ¥218.50 |
| Final price | ¥186.40 |
| Cumulative delta | **−14.7%** (R0 +5.32%, R1 −10%, R2 −10%) |
| Info cascade events | **3** (all from `mutual_fund_active_pm`) |
| Strategic signals | 5 (all 5 strategic personas, all neutral/low) |
| Compliance | PASS |

### Run 2 — NVIDIA Q1 2026 (2 rounds, US equity, cross-market validation)

| Field | Value |
|---|---|
| Simulation ID | `concordia_6a13147c850f` |
| Personas | 10 (`personas/us-equity-v1.yaml`) |
| Rounds | 2 |
| Wall clock | 6.2 seconds |
| Cost (USD) | $0.0029 |
| Initial price | $177.77 |
| Final price | $215.10 |
| Cumulative delta | **+21%** (R0 +10%, R1 +10%, both rounds hit the cap) |
| Info cascade events | 0 (us-equity persona pack doesn't tag any persona with `emits_info_events` yet) |
| Strategic signals | 0 (us-equity persona pack has no strategic-mode personas) |
| Compliance | PASS |

## What this proves

1. **The Concordia engine runs end-to-end on a real LLM with real personas.**
   No crashes, no hangs, no compliance violations.
2. **The cost tracker + budget guard + compliance filter all survive the rewrite.**
   Cost is correctly attributed to the cost_tracker singleton from both the
   `concordia_lm` (sync) path and the `chat_json_sync` (sync) path.
3. **The information cascade actually fires.** `mutual_fund_active_pm` broadcast
   3 sentence-length notes across the 3 rounds in the BYD run, and those notes
   landed in the next round's observation block for every other entity.
4. **The strategic-layer signal works as a parallel track.** All 5 strategic
   personas in the BYD run produced direction/magnitude/horizon signals that
   the report's "Strategic Layer" section renders separately from the price
   trajectory.
5. **Cross-market still works.** NVIDIA on `personas/us-equity-v1.yaml` runs
   without modification because the engine is currency-agnostic and λ is
   resolved from `event.market`.

## What this does NOT yet prove

1. **Real T+1 / T+5 verification** — that's what scorecard #003 was for, and
   it remains pending until 2026-04-30 (T+1) and 2026-05-04 (T+5). Phase H
   doesn't change the prediction methodology in a way that invalidates #003.
2. **The cascade actually changes prices.** The current smoke had 3 cascade
   events but they're all roughly the same content ("BYD Q1 earnings show
   strong revenue growth but slight margin pressure"). To demonstrate that
   the cascade matters, we'd need a 5+ round run where one persona broadcasts
   something that visibly changes another persona's order flow downstream.
   That's a Phase I scoping question.
3. **Multiple cascade types.** Phase H ships exactly one — `InfoEvent`
   (1-sentence broadcast). PriceAlert, MarginCall, RegulatoryWarning are
   Phase I.

## Test suite state

| Test file | Tests | Status |
|---|---|---|
| `tests/test_market_dynamics.py` | 16 | PASS |
| `tests/test_concordia_lm.py` | 11 | PASS |
| `tests/test_concordia_engine.py` | 12 | PASS |
| `tests/test_persona.py` | (existing) | PASS |
| `tests/test_persona_factory.py` | (existing) | PASS |
| `tests/test_output_filter.py` | (existing, 1 test rewired to ConcordiaSimResult) | PASS |
| **Total** | **146** | **PASS** |

## Files actually deleted (verification)

```bash
$ ls src/ssfish/sandbox.py tests/test_sandbox.py
ls: cannot access 'src/ssfish/sandbox.py': No such file or directory
ls: cannot access 'tests/test_sandbox.py': No such file or directory
```

## Followups

- Tag 2-3 personas in `personas/us-equity-v1.yaml` and `personas/crude-oil-wti-v1.yaml`
  with `emits_info_events: true` so cascade dynamics work cross-market
- Stress-test cascade variety: run a sim where the cascade events differ
  meaningfully across rounds (this requires the LLM to actually produce
  different content each round, which it usually doesn't with `seed=42`)
- T+5 verification of scorecard #003 still pending
- Phase I scoping: more cascade types? Cross-asset spillover? Or pivot to
  OSS launch (Approach C)?
