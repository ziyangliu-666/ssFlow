# ssFish Persona Pack Schema (v3)

This document defines the YAML schema used by `personas/*.yaml` files.
The current and only supported version is **schema_version: 3**.

For a starter file you can copy and edit, see `personas/_template.yaml`.
For a full hand-written example, see `personas/ashare.yaml` (30 entities:
14 traders + 16 information entities).

## What changed in v3 (vs v2)

v2 assumed every persona was a trader. v3 introduces the **information
ecosystem** alongside the trading layer — some personas don't trade at all,
they only publish content (news wires, analysts, regulators, KOLs, company
IR). Three new fields drive this:

- `entity_role` — `trader | media | analyst | regulator | policy | kol | news_wire | company_ir`
- `follows` — list of persona ids this persona reads in its feed
- `publishes` — list of content types this persona can emit

And the `sandbox` block (order execution config) is now **optional**. Traders
still require it; non-traders must NOT have it.

## Top-level structure

```yaml
schema_version: 3
market: <slug>           # required: ashare | us-equity | crude-oil-wti | ...
locale: <ietf-tag>       # required: zh-CN | en-US | ja-JP | ...
last_updated: YYYY-MM-DD # required: when the data sources were last refreshed

data_sources:            # required: at least 1 entry
  - id: <unique-id>
    name: <human readable>
    org: <publishing org>
    url: <citation url>
    accessed: YYYY-MM-DD
    note: <optional 1-line caveat>

personas:                # required: at least 1 entry
  - id: <unique persona id within this pack>
    archetype: <human label>
    sub_archetype: <optional, more specific>
    display_name: <one-line description>
    decision_mode: discretionary | systematic | strategic | passive
    role: <free-form, recommended values below>

    # Phase I additions — the information ecosystem layer
    entity_role: trader | media | analyst | regulator | policy | kol | news_wire | company_ir
    follows: [other_persona_id, ...]      # who this persona reads
    publishes:                             # what content this persona can emit
      - content_type: news_brief | research_note | policy_statement |
                      regulatory_inquiry | social_post | company_announcement
        style_hint: "短句描述 LLM 语气"
        trigger_prob: 0.3            # base probability of emitting per round
        authority_weight: 0.6        # feed-ranking priority (0-1)
        max_length_chars: 240

    market_share:        # required
      by_volume: <0-1>
      by_holdings: <0-1>
      by_account_count: <0-1>
      citations:
        - source_id: <data_source id>
          note: <optional>

    voice_prompt: |                       # required: in-character description
      Multi-paragraph speech + behavior guidelines for this persona class.

    biases:              # optional
      loss_averse: 0.8
      herd_following: 0.7

    # Trading sandbox — REQUIRED for entity_role=trader, must be absent otherwise
    sandbox:
      instance_count: 1000              # stochastic agents to spawn for this class
      capital_distribution:
        type: lognormal
        median_cny: 200000
        sigma: 0.6
      initial_position_distribution:
        type: bernoulli
        prob_holding: 0.50
      risk:
        max_position_pct: 0.95
      action_space:                      # LLM picks a distribution over these
        - {name: hold,           side: none, pool: none,                fraction: 0.0}
        - {name: panic_sell_50pct, side: sell, pool: holdings_in_target, fraction: 0.5}
        - {name: fomo_buy_30pct,  side: buy,  pool: cash,                fraction: 0.3}
```

## Entity roles

| entity_role | Has sandbox? | Typical content types | Example archetypes |
|---|---|---|---|
| `trader` | ✅ required | optional: research_note, social_post | 散户, 公募, 私募, 量化, 险资, 产业资本, 国家队 |
| `media` / `news_wire` | ❌ | news_brief | 财联社, 新华社, Bloomberg, 第一财经 |
| `analyst` | ❌ | research_note | 中信建投, 中金, 国信 卖方分析师 |
| `regulator` | ❌ | regulatory_inquiry | 证监会 |
| `policy` | ❌ | policy_statement | 央行, 发改委, 工信部 |
| `kol` | ❌ | social_post | 雪球大 V, 微博财经博主, 公众号作者 |
| `company_ir` | ❌ | company_announcement | 上市公司 IR 部门 |

Only trader personas actually place orders that flow into the Kyle price
impact calculation. Non-trader personas exist entirely to publish content
into the OASIS social stream; traders read filtered feeds and base their
trading decisions on what they've seen.

## Follow graph

Each persona's `follows` list drives which posts show up in its feed. Two
special values:

- `"*"` — "follows everyone in the pack". Used sparingly for news wires
  and aggregators.
- `"__market__"` — "follows the synthetic market-event broadcaster" (the
  agent that posts price updates after each round). Auto-added to all
  traders by the engine; you don't need to list it explicitly.

Any other string must be the `id` of another persona in the same pack.
The YAML loader validates forward references and fails loudly on typos.

Example (a 散户追涨 persona):

```yaml
follows:
  - news_wire_cailianshe      # reads financial news
  - news_wire_yicai            # reads mainstream finance media
  - retail_kol_xueqiu_laoqin   # reads retail KOLs
  - retail_kol_weibo_caijing
  # does NOT follow analysts or policy makers (short-term retail doesn't read research)
```

A 公募基金 PM persona typically follows 10+ entities (institutional info
diet). A quant persona might only follow 2-3 news wires. Tune the follows
list to the information behavior of the real-world participant class.

## Content types

| content_type | Typical author | Length | Authority weight | Example |
|---|---|---|---|---|
| `news_brief` | media / news_wire | 1-3 sentences | 0.6-0.8 | "BYD Q1 营收 +18% beat" |
| `research_note` | analyst | 3-6 sentences | 0.7-0.9 | "DCF 参考区间 230-250" |
| `policy_statement` | policy | 1-3 sentences | 0.85-0.95 | "发改委就产业政策表态" |
| `regulatory_inquiry` | regulator | 1-2 sentences | 0.9-0.95 | "交易所发问询函" |
| `social_post` | kol / trader | 1-2 sentences | 0.3-0.6 | "股海老钱: 这波机构接力" |
| `company_announcement` | company_ir | 1-3 sentences | 0.9-1.0 | "公司发布业绩公告" |

`trigger_prob` controls how often each LLM round emits this content type.
Default 0.3 is a good starting point. News wires with `"*"` follows and
high trigger_prob (0.5-0.6) publish most rounds; regulators with
trigger_prob 0.08-0.12 publish rarely but with high authority.

## Trading tool on traders (the unified-decision architecture)

The engine injects a CAMEL `FunctionTool` called `submit_order_distribution`
into every trader's OASIS `SocialAgent`. Trader LLMs see this tool
alongside the 21 native OASIS social actions (create_post, repost, follow,
...) in a single tool-calling decision. When the LLM calls
`submit_order_distribution`, the arguments are captured into a per-sim
`OrderCollector`; after `env.step()`, the engine drains the collector and
applies each trader's distribution via `apply_distribution_to_agent_pop`.

**This means the trader's social posts and trading decisions come from
the SAME CAMEL ChatAgent memory.** A trader that posts "bullish on BYD"
will remember that post next round when it decides whether to buy or sell.

## Validation

`src/ssfish/persona.py:load_personas()` enforces these rules at load time:

1. `schema_version` must be exactly `3`
2. `market`, `locale`, `last_updated`, `data_sources`, `personas` all required
3. Each persona must have `id`, `archetype`, `display_name`, `voice_prompt`,
   `decision_mode`, `role`, `market_share`
4. `market_share` must set at least one of `by_volume` / `by_holdings` /
   `by_account_count`
5. `entity_role == "trader"` → `sandbox` block **required**
6. `entity_role != "trader"` → `sandbox` block **must be absent**
7. Every `follows` entry must reference a known persona id in the same pack
   (or `"*"` / `"__market__"`)
8. Every `publishes[].content_type` must be in the whitelist
9. `decision_mode` must be one of `discretionary / systematic / strategic / passive`
10. Persona ids must be unique within the pack

Validation errors raise `PersonaSchemaError` with the offending persona id
+ source file path.
