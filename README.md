# ssFish — 市场事件信息生态推演引擎

Market event simulation engine where a cast of ~30 AI agents (traders + media +
analysts + regulators + policy makers + KOLs) read each other's posts, react,
and trade. The price comes out the back as a function of order flow. You feed
in a market event; you get back a round-by-round narrative of who said what
and what happened to the price.

**这不是投资建议工具。** 所有输出都经过合规过滤器, 严格不含具体的买卖建议或目标价。
详见 `src/ssfish/output_filter.py` 的合规防火墙。

## What it actually does

Give it a piece of text — a news headline, an earnings summary, a policy
rumor — and it will:

1. **Stage 0 extract**: auto-detect the market / instrument / event type /
   current price / ADV from the input, using web search + LLM classification.
2. **Build a 30-agent information ecosystem** from `personas/ashare.yaml`:
   14 trader classes (散户追涨, 公募基金, 私募, 量化, 险资, 北上, 产业资本,
   国家队, etc.) + 16 non-trading information entities (财联社, 新华社,
   Bloomberg 中国, 中信建投/中金/国信 分析师, 证监会, 央行, 发改委, 工信部,
   雪球大 V, 微博财经博主, 上市公司 IR, 中汽协, ...).
3. **Run 5 rounds on top of OASIS (CAMEL-AI's social simulation framework)**:
   each round, every agent takes one turn via CAMEL's tool-calling LLM — it
   can post, repost, follow, like, comment, AND (for traders) call our
   custom `submit_order_distribution` tool in the same decision. One brain,
   one memory, social and trading come from the same CAMEL `ChatAgent` turn.
4. **Feed routing**: each trader only sees posts from agents it `follows`
   (from the YAML config). Retail追涨 reads news wires + KOLs; 公募 PM reads
   everyone; 量化 reads only news wires. Information asymmetry is real.
5. **Price formation**: net trader flow this round feeds into the Kyle
   square-root price impact formula (capped at ±10% per round modeling A-share
   涨跌停板). The new price is posted back into OASIS as a `__market__` agent
   post, where every trader sees it in next round's feed.
6. **Output**: a feed-first narrative markdown report where each round shows
   the publications grouped by content type, the order flow with rationales,
   and the price update. Plus per-class P&L at the final price.

The typical output reads like a financial media feed, not a spreadsheet.

## Architecture (Phase II)

```
                  event text / URL
                         │
                         ▼
               ┌─────────────────────┐
               │  event_extractor    │  (LLM + web search,
               │   (Stage 0)         │   auto-fills market / price / ADV)
               └─────────┬───────────┘
                         │
                         ▼
               ┌─────────────────────┐
               │ personas/ashare.yaml│  (30 entities, follow graph)
               │  schema v3           │
               └─────────┬───────────┘
                         │
                         ▼
        ┌────────────────────────────────────┐
        │  oasis_engine.run_simulation       │  (async, 5 rounds)
        │                                     │
        │  ┌──────────────────────────────┐  │
        │  │ OASIS env.step()             │  │
        │  │ — 30 agents act in parallel  │  │
        │  │ — each picks tool calls from │  │
        │  │   {21 social + submit_order} │  │
        │  │ — traders have max_iter=2    │  │
        │  └──────────────┬───────────────┘  │
        │                 │                    │
        │                 ▼                    │
        │  ┌──────────────────────────────┐  │
        │  │ drain OrderCollector         │  │
        │  │ apply_distribution_to_agent  │  │
        │  │  _pop for each trader        │  │
        │  │ → Kyle price impact          │  │
        │  └──────────────┬───────────────┘  │
        │                 │                    │
        │                 ▼                    │
        │  ┌──────────────────────────────┐  │
        │  │ post price update back into  │  │
        │  │ OASIS as __market__ agent    │  │
        │  └──────────────────────────────┘  │
        └────────────────────┬───────────────┘
                             │
                             ▼
                   ┌─────────────────┐
                   │  report.py       │
                   │  render_simulation_markdown
                   │  (feed-first narrative)
                   └─────────┬───────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ output_filter         │
                  │ assert_compliant      │
                  │ (regex forbidden vocab)│
                  └─────────┬────────────┘
                             │
                             ▼
          reports/{sim_id}.md + scorecard.db + oasis_dbs/*.db
```

- **engine**: `src/ssfish/oasis_engine.py` (main async `run_simulation`)
- **social layer**: OASIS (`camel-oasis` 0.2.5, pulled from PyPI, unforked)
- **trading layer**: `src/ssfish/trading_layer.py` (pure-Python,
  framework-agnostic — spawn agents, apply actions, Kyle)
- **LLM adapter**: `src/ssfish/oasis_lm.py` (CAMEL `OpenAICompatibleModel`
  subclass routing through our `cost_tracker` + `output_filter`)
- **trading tool**: `src/ssfish/oasis_trading_tool.py` (`OrderCollector` +
  `submit_order_distribution` FunctionTool injected into each trader's
  `SocialAgent`)
- **persona adapter**: `src/ssfish/oasis_persona_adapter.py` (YAML → OASIS
  `AgentGraph` with follow edges + synthetic `__market__` broadcaster)
- **feed reader**: `src/ssfish/oasis_feed_reader.py` (queries OASIS's SQLite
  db, filters by follow graph, returns list[Publication])
- **persona packs**: `personas/ashare.yaml` (30 entities, hand-tuned
  follow graph), `personas/us-equity-v1.yaml`, `personas/crude-oil-wti-v1.yaml`
  (sketches)
- **report renderer**: `src/ssfish/report.py` (feed-first narrative markdown)
- **web UI**: `api/app.py` + `web/index.html` (Flask, password-auth, two-step
  form: free-form input → auto-extract → confirm → run)
- **CLI**: `scripts/run_one.py`
- **scorecard**: `src/ssfish/scorecard.py` (SQLite v5, tracks every sim
  run + publication log)
- **tests**: `tests/` — 217 tests, compliance filter + schema loader are
  launch blockers

## Quickstart

```bash
# 1. Install dependencies
uv sync --extra dev

# 2. Configure secrets
cp .env.example .env
$EDITOR .env
#   OPENAI_API_KEY=<yourapi.cn key>
#   OPENAI_BASE_URL=https://yourapi.cn/v1
#   SSFISH_DEFAULT_MODEL=gpt-4o-mini
#   SSFISH_PASSWORD=<flask basic auth password>
#   SSFISH_BUDGET_USD=5.0

# 3. Run tests (should be 217 passing)
uv run pytest -q

# 4a. Run a simulation from the command line (free-form input mode)
SSFISH_BUDGET_USD=5.0 uv run python scripts/run_one.py \
    --input "BYD Q1 2026 财报: 营收 +18% beat, 毛利率 -2.3pp miss" \
    --confirm

# 4b. Or run the Flask web UI
SSFISH_BUDGET_USD=5.0 uv run python -c \
    "from api.app import app; app.run(host='127.0.0.1', port=5000)"
# Browser: http://127.0.0.1:5000
# Paste event text → click "分析事件" → review extracted fields → click "运行"
```

### Explicit parameter mode (skip Stage 0 extractor)

```bash
SSFISH_BUDGET_USD=5.0 uv run python scripts/run_one.py \
    --event /dev/stdin \
    --ticker 002594 \
    --event-type earnings \
    --event-date 2026-04-29 \
    --personas personas/ashare.yaml \
    --current-price 218.50 \
    --adv 8000000000 \
    --rounds 5 <<'EOF'
BYD Q1 2026 earnings: 营收 +18% beat consensus 12%, 毛利率 -2.3pp miss,
汽车业务销量 +15% YoY, 海外占比提升至 30%.
EOF
```

Cost per 5-round simulation with the full 30-persona pack: roughly $0.08-0.15
on `gpt-4o-mini`. Wall clock: ~60-90 seconds.

## Persona pack authoring

The main pack is `personas/ashare.yaml` (30 entities, hand-tuned). To create
a new pack for a different market:

```bash
cp personas/_template.yaml personas/my-market.yaml
$EDITOR personas/my-market.yaml
uv run python -c "from ssfish.persona import load_personas; print(len(load_personas('personas/my-market.yaml')))"
```

See `personas/SCHEMA.md` for the full field reference.

There's also a calibration pipeline that auto-generates persona packs via web
research (`scripts/generate_pack.py`):

```bash
uv run python scripts/generate_pack.py --market us-equity \
    --output personas/us-equity-auto.yaml
```

## Python version + framework pins

- Python **>=3.12, <3.13** (camel-oasis pulls `tiktoken==0.7.0` which has no
  Python 3.13 wheels; `uv python pin 3.12`)
- `camel-oasis>=0.2.5` (unforked — we use the official `tools` parameter and
  `FunctionTool` extension mechanism, not a fork)
- `openai>=1.50.0` (both sync and async clients)

## ⚠️ Compliance

This is an **event research tool**, not an investment advice tool. All
output passes through `output_filter.assert_compliant` before display. Any
generated content containing forbidden vocabulary (建议 / 推荐 / 应该 / 买入 /
卖出 / 目标价 / 评级 / BUY / SELL / target price / recommend) gets
automatically quarantined and replaced with a verification placeholder.

Using this tool means you understand: the simulated price trajectories are
thought experiments, not predictions; the per-class P&L is a simulation
artifact, not advice to act.

## License

MIT. See `LICENSE`.

## Project history

See `scorecard/entries/` for the complete rollout log:
- `001-byd-2026q1-earnings.md` — first sentiment-mode prediction (MVP era)
- `003-byd-sandbox-vs-real.md` — first agent-based Kyle-formula prediction
  (T+5 verification pending 2026-05-04)
- `004-phase-h-concordia-rewrite.md` — Concordia-based engine rollout (later deleted)
- `005-phase-i-oasis-rewrite.md` — OASIS engine rollout + 30-persona expansion
- `006-phase-ii-unified-decision.md` — unified social + trading decision via
  CAMEL FunctionTool extension (current architecture)
