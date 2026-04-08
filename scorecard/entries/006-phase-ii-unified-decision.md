# Scorecard #006 — Phase II Unified-Decision Refactor

> **Status:** Engine refactor validated end-to-end on a real LLM smoke test.
> **Recorded:** 2026-04-08 (UTC+8)
> **Mode:** oasis (Phase II — unified social + trading decision via
> CAMEL FunctionTool, no OASIS fork)

## Why this entry exists

Phase I shipped the OASIS-based information ecosystem with a known coherence
gap: each trader persona made **two independent LLM calls per round** — one
inside OASIS's social step and another inside our separate
`trading_layer.decide_orders` path. A trader could post "bullish on BYD" in
the social step and then independently decide to sell in the trading step.
Not coherent, not how real traders work.

**Better solution found in OASIS source — no fork needed.** Two lines of
`agent.py`:

```python
# oasis/social_agent/agent.py:68
tools: Optional[List[Union[FunctionTool, Callable]]] = None,

# oasis/social_agent/agent.py:145
if action_name not in ALL_SOCIAL_ACTIONS:
    agent_log.info(...)
```

`SocialAgent.__init__` accepts an extra `tools` parameter that gets merged
with the 21 built-in social tools before being passed to CAMEL's ChatAgent.
And `perform_action_by_llm` explicitly handles non-social tool calls —
CAMEL has already executed them, OASIS just logs and moves on. **Officially-
supported extension mechanism.**

Phase II uses this. One LLM call per trader per round, CAMEL picks from
`{21 social tools} ∪ {submit_order_distribution}` in a single coherent
decision, trader memory contains both the social posts and the trading
decisions in the same timeline.

## What changed

| File | Change | LOC |
|---|---|---|
| `src/ssflow/oasis_trading_tool.py` (new) | `OrderCollector` + `make_submit_order_tool` | ~210 |
| `src/ssflow/trading_layer.py` | Extract `apply_distribution_to_agent_pop` pure-math core | +90 / −5 |
| `src/ssflow/oasis_persona_adapter.py` | Accept `order_collector`, inject tool per trader, trader-specific profile text, `max_iteration=2` for traders | +60 / −3 |
| `src/ssflow/oasis_engine.py` | Remove separate `decide_orders` path, drain collector after `env.step()`, hold-fallback for traders that didn't call the tool | +40 / −45 |
| `tests/test_oasis_trading_tool.py` (new) | 15 tests for OrderCollector + FunctionTool + apply_distribution | ~260 |

## Key design decisions

### 1. `OrderCollector` is a thread-safe per-sim dict

OASIS runs agents concurrently via asyncio. Each trader's
`submit_order_distribution` tool is a closure with `persona.id` captured,
pointing at the same shared `OrderCollector`. When multiple agents call the
tool in parallel, `add()` takes a lock. After `env.step()` completes, the
engine calls `drain()` to atomically snapshot + clear pending orders.

### 2. Trader-specific system message

OASIS's `perform_action_by_llm` hardcodes a user prompt saying "Please
perform **social media actions**". Combined with the default profile
description, the LLM strongly prefers social tools and ignores
`submit_order_distribution`. Fix: inject explicit trading instructions into
the trader's `user_profile` text (part of the system message):
- "除了社交动作, 你**每一轮都必须**调用 submit_order_distribution"
- Exact list of valid action names for this persona's action_space
- Concrete example of the JSON distribution shape
- Hold fallback guidance (even observing → call tool with hold-dominated dist)

Counterbalances OASIS's hardcoded "do social stuff" user prompt.

### 3. `max_iteration=2` for traders

CAMEL's ChatAgent defaults to 1 iteration per step. Empirically many LLMs
pick ONE tool (usually `create_post`) and stop. Setting `max_iteration=2`
for traders lets CAMEL do a follow-up iteration: the LLM sees the first
turn's tool results fed back into its context and gets another chance to
call any remaining tools — usually `submit_order_distribution`.

In the smoke test this raised trader tool-call rate from 2/14 → 10/14 in
a single round. Info entities stay at `max_iteration=1` (they only have
social tools, no need for a second turn).

### 4. Hold fallback in the engine

Not every trader calls the tool every round even with the above — strategic
holders especially tend to observe. The engine treats absence as an
explicit hold: builds `{hold_action: 1.0}` distribution and feeds it
through `apply_distribution_to_agent_pop` so the trader's agent population
still updates each round with 0 net flow. Keeps P&L calculation consistent.

## Real LLM smoke test results

**Run**: BYD Q1 2026, 30 personas, 2 rounds, Phase II engine

| Metric | Phase I (separate) | Phase II (unified) | Delta |
|---|---|---|---|
| Simulation ID | `oasis_d674ee938dfc` | `oasis_bc2e31d684bc` | |
| Total LLM cost | $0.0212 | **$0.0299** | +41% |
| Wall clock | ~3 min | **42.1 seconds** | **-77%** |
| Total LLM calls | 89 | 209 | +135% |
| Publications emitted | 38 | 62 | +63% |
| Traders calling submit_order (R1) | N/A (separate path) | **10 / 14** | ✅ |
| Social + trading coherence | Two separate calls | **One unified decision** | ✅ |
| Price trajectory | ¥218.50 → ¥264.39 | ¥218.50 → ¥264.39 | Same (both capped) |
| Compliance | PASS | **PASS** | ✅ |

**Why cost went up +41%**: `max_iteration=2` + longer trader system messages
(the extra trading instructions).

**Why wall clock went DOWN from ~3 min to 42 seconds**: Phase I serialized
the trading layer AFTER the OASIS step (per-trader `chat_json_sync` calls
via ThreadPoolExecutor). Phase II moves trading decisions INTO the OASIS
social step, which OASIS parallelizes natively. Net result: faster despite
more LLM calls.

**Sample rationales (unified-decision path, R1)**:
- 国家队 (+¥8.40億): "对于比亚迪 Q1 的分析中提到毛利率变化, 这很重要. 市场需要稳定, 短期可能影响投资情绪."
- 险资/社保/养老 (+¥5.65億): "The analysis of BYD's earnings emphasizes revenue growth while highlighting a concerning margin drop. This supports a strategy of holding for long-term stability."
- 量化 (+¥3.90億): "BYD Q1财报中的毛利率变化, 引发市场的关注, 考虑到这可能影响后市的趋势, 因此会倾向于顺应市场趋势进行动量跟随."
- 私募基金 (+¥3.19億): "看到关于BYD毛利率下滑的分析, 虽然收入超预期, 但这可能影响未来股价走势..."
- 上市公司互持 (-¥2.05億): "观察到市场对比亚迪第一季度的收入超出预期, 但毛利率下滑, 因此作为战略持有者, 我会重新分享这条信息..."

Every rationale references either specific post ids (`post:21`, `post:29`)
or the seeded event. These traders also made **social posts** visible in
the Publications list — and the social posts + trading decisions come from
the **same CAMEL agent memory**. Coherence is now a structural property.

## What this proves

1. **No OASIS fork needed.** The `tools` parameter + `ALL_SOCIAL_ACTIONS`
   dispatch bypass in `SocialAgent` is the intended extension mechanism.
   ssFlow adds 0 lines to OASIS source.

2. **Social + trading are one decision.** The trader's CAMEL ChatAgent
   memory contains both the social actions and the trading decisions in
   the same timeline.

3. **`max_iteration=2` is load-bearing.** Without it, many LLMs pick one
   social tool and stop. With it, they reliably get to the trading tool
   on the second iteration.

4. **Hold-fallback is clean.** Traders that don't call the tool get a
   synthetic all-hold distribution so their agent populations still update
   each round. No special-casing.

## Test suite state

| Test file | Tests | Status |
|---|---|---|
| `tests/test_oasis_trading_tool.py` (new) | 15 | PASS |
| `tests/test_trading_layer.py` | 27 | PASS (decide_orders now delegates to apply_distribution_to_agent_pop) |
| `tests/test_oasis_persona_adapter.py` | 14 | PASS |
| `tests/test_oasis_feed_reader.py` | 16 | PASS |
| `tests/test_oasis_lm.py` | 10 | PASS |
| ... (others) | 135 | PASS |
| **Total** | **217** | **PASS** |

## Followups (not Phase II scope)

- **Raise trader tool-call rate to 14/14**. Currently ~10/14 in R1.
  Options: `max_iteration=3`, stronger system message, tool docstring.
  Diminishing returns past 10/14 since strategic holders realistically
  shouldn't act every round.
- **Polish content_type registration** (deferred from Phase I). Most
  social posts still come back as `social_post` content_type because the
  engine can't map tool-call-driven posts back to PublishConfig metadata.
- **Run a 5-round BYD with external_events**. The 2-round smoke proves
  the architecture; a 5-round run with an injected R3 policy event would
  demonstrate the full narrative arc.
- **T+5 verification of scorecard #003** still pending (2026-05-04).
