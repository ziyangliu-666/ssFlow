# ssFish Persona Pack Schema (v2)

This document defines the YAML schema used by `personas/*.yaml` files.
The current and only supported version is **schema_version: 2**.

For background on why this schema looks the way it does, see
`docs/persona-pack-spec-v1.md` (the design doc).

For a starter file you can copy and edit, see `personas/_template.yaml`.

## Top-level structure

```yaml
schema_version: 2
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
  - id: <unique persona id>
    archetype: <human label>
    sub_archetype: <optional, more specific>
    display_name: <one-line description>
    decision_mode: discretionary | systematic | strategic | passive
    role: <free-form, recommended values below>
    voice_prompt: |
      <multi-paragraph in-character description>
    market_share:
      by_volume: <0-1>
      by_holdings: <0-1>
      by_account_count: <0-1>
      citations:
        - source_id: <data_source id from above>
    sandbox:
      instance_count: <int>
      capital_distribution: { type: ..., ... }
      initial_position_distribution: { type: ..., ... }
      risk: { max_position_pct: ..., ... }
      action_space: [ { name, side, pool, fraction }, ... ]
      reaction_lag_rounds: { type: discrete, values: [...] }
```

## Required fields per persona

| Field | Type | Notes |
|---|---|---|
| `id` | string (slug) | Must be unique within the pack. Used for cross-round agent state. |
| `archetype` | string | Human-readable category label. |
| `display_name` | string | One-line description shown in reports. |
| `voice_prompt` | string (multi-line) | Sent to the LLM as the system prompt. |
| `decision_mode` | enum | One of `discretionary`, `systematic`, `strategic`, `passive`. |
| `role` | string | Free-form but recommended values: `directional_speculator`, `long_term_holder`, `strategic_holder`, `commercial_hedger`, `market_maker_arb`, `passive_market_maker`, `quant`, `sell_side_analyst`, `short_seller`, `contrarian_short`, `active_long_short`, `institutional_long_only`, `institutional_long_horizon`, `foreign_active`, `passive_buyer`. |
| `market_share` | dict | Must set at least one of `by_volume` / `by_holdings` / `by_account_count`. |

## Optional fields per persona

| Field | Type | Default | Notes |
|---|---|---|---|
| `model` | string \| null | null | Specific LLM model for this persona. None falls back to `settings.default_model`. |
| `sub_archetype` | string | null | More specific category, e.g., `retail_active_meme`, `commercial_hedger_consumer`. |
| `time_horizon_days` | [min, max] | null | Typical holding period for this class. |
| `capital_range_cny` | [min, max] | null | Typical wealth range. |
| `behavior` | dict | null | `avg_position_pct`, `annual_turnover`, `typical_holding_period_days`, `stop_loss_discipline`, `reaction_speed`. |
| `information` | dict | null | `primary_sources`, `secondary_sources`, `ignored`, `english_capable`. |
| `biases` | dict | {} | Key-value pairs of `bias_name: 0.0-1.0` strength. Surfaced into the LLM system prompt. |
| `knowledge` | dict | {} | `holdings`, `information_sources`, `ignores`. Surfaced into the LLM system prompt. |
| `sandbox` | dict | null | Required only for sandbox-mode runs. See below. |
| `strategic_signal_schema` | dict | null | Required for strategic personas; auto-defaulted if `decision_mode == strategic` and the field is missing. |
| `contributes_to_sentiment_mean` | bool | true (false for strategic) | Whether this persona is included in `sentiment_mean` aggregation. |
| `contributes_to_strategic_signal` | bool | false (true for strategic) | Whether this persona emits a `strategic_signal` field. |

## `sandbox` block (required for sandbox-mode runs)

```yaml
sandbox:
  instance_count: <int>           # Number of agent instances spawned per sim
  capital_distribution:
    type: lognormal | uniform | fixed
    median_cny: <float>           # for lognormal
    sigma: <float>                # for lognormal (log-scale spread)
    floor_cny: <float>            # optional clipping
    ceiling_cny: <float>          # optional clipping
    min_cny: <float>              # for uniform
    max_cny: <float>              # for uniform
    value_cny: <float>            # for fixed
  initial_position_distribution:
    type: bernoulli | fixed | none
    # bernoulli:
    prob_holding: <0-1>           # fraction of agents that hold the target
    position_size_pct_when_holding:
      type: uniform | fixed
      min: <0-1>
      max: <0-1>
      value: <0-1>                # for fixed
    # fixed:
    value: <0-1>                  # fraction of capital in target
  risk:
    max_position_pct: <0-1>       # HARD CAP enforced by apply_action_to_agent
    margin_account_pct: <0-1>     # informational, not enforced in v1
    leverage_max: <float>         # informational
    stop_loss_threshold: <float>  # negative %, informational
    stop_loss_discipline: <0-1>   # probability that stop is actually executed
  action_space:                   # Gotcha 1 lock-in: dicts not bare strings
    - name: <unique action name>
      side: none | buy | sell
      pool: none | cash | holdings_in_target
      fraction: <0-1>             # of pool consumed
  reaction_lag_rounds:
    type: discrete
    values: [<int>, ...]          # informational; not yet used by run_sandbox_simulation v1
```

## Aggregation flag interactions

- For `decision_mode == strategic`, the loader auto-sets:
  - `contributes_to_sentiment_mean = false`
  - `contributes_to_strategic_signal = true`
- These can be overridden explicitly in the YAML if needed.
- Strategic personas STILL participate in sandbox order flow (they submit orders rarely
  but with large notional). They are excluded from sentiment-mode aggregation but
  contribute to sandbox-mode price impact.

## Compliance constraints

The output filter (`src/ssfish/output_filter.py`) regex-blocks a list of forbidden
investment-advice phrases. The persona's `voice_prompt` and any free-form text the
LLM produces will be sanitized before rendering. To avoid quarantine:

- Do NOT use 买入/卖出/建议/应该/必须/减仓/加仓/建仓/清仓/目标价/评级/止损位/止盈位
  in voice_prompts.
- Use descriptive vocabulary instead: 进入/离场/倾向/我会先观望/分批减/加仓位/合规窗口.
- For action_space names, prefer descriptive labels like `panic_sell_50pct`, not
  `bear_signal_recommend`.

## Sanity checks the loader runs

- `id` is unique within the file
- `decision_mode` ∈ `{discretionary, systematic, strategic, passive}`
- `market_share` sets at least one dimension
- `sandbox.action_space` is a non-empty list of dicts each with at least a `name`
- Numeric `market_share` values are coercible to float

## What the loader does NOT validate (yet)

- Sum of `by_volume` across personas should be ~1.0 (currently a soft expectation)
- Sum of `by_holdings` should be ~1.0
- Action space `pool` field consistency with the action's `side`
- `voice_prompt` sanitization at load time

These are deferred to ssFish v3 schema migration.

## Examples

See `personas/ashare.yaml` (14 personas, the canonical pack), 
`personas/us-equity-v1.yaml` (10 personas, sketch),
and `personas/crude-oil-wti-v1.yaml` (10 personas, commodity sketch).

## How to add a new market

1. Copy `personas/_template.yaml` to `personas/<your-market>.yaml`
2. Fill `market`, `locale`, `last_updated`, and `data_sources` (at least 2 real
   public citations from regulators or established research orgs)
3. Author 8-15 personas covering the major participant classes in your market
4. Verify the file loads with `uv run python -c "from ssfish.persona import load_personas; load_personas('personas/<your-market>.yaml')"`
5. Run a smoke test: `uv run python scripts/run_one.py --personas personas/<your-market>.yaml --mode sandbox ...`
6. Cite this file when publishing the pack
