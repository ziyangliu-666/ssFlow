"""ssFlow Persona → OASIS AgentGraph adapter.

Phase I — bridges our YAML persona schema (v3) to OASIS's social-agent
graph. The OASIS environment expects an `AgentGraph` of `SocialAgent`
instances connected by directed follow edges, plus a shared `Channel` for
the platform layer to route messages through.

Key design decisions:

  - Every persona becomes one OASIS `SocialAgent`. Trader personas AND pure-
    information personas (media, analysts, regulators, KOLs) both get an
    OASIS agent — the difference is what `available_actions` they're allowed
    to take, and whether the trading layer also runs them through Kyle.

  - Persona ids are strings; OASIS uses sequential integer agent ids.
    `build_agent_graph` returns BOTH the graph and a `persona_id_to_oasis_id`
    mapping so the engine can correlate the two namespaces.

  - We use the **Twitter** platform (recsys_type="twitter") rather than Reddit
    because the Reddit `to_reddit_system_message` requires `gender`/`age`/
    `mbti`/`country` profile keys we don't model. Twitter only needs
    `user_profile`, which maps cleanly to our `voice_prompt`.

  - The follow graph is built from each persona's `follows` list. Special
    values: `*` means "follow every other persona in the pack", `__market__`
    means "follow the synthetic market-event broadcaster" (auto-added to all
    traders by this adapter regardless of whether they list it explicitly).

  - A synthetic `__market__` agent is added at the end and followed by all
    trading personas. The Phase I engine uses this agent to broadcast price
    updates as `CREATE_POST` events, so price feedback flows through the same
    OASIS feed mechanism as everything else.

  - `available_actions` is derived per persona:
      • Trader personas: full social-action set (post, repost, follow, like,
        comment, etc.) — they're real social actors
      • Pure-info personas (media/analyst/regulator/etc): mostly CREATE_POST
        + LIKE_POST + REPOST + DO_NOTHING. They can't follow/unfollow at runtime
        because their follow graph is static-from-yaml.
"""

from __future__ import annotations

import logging
from typing import Optional

from camel.models.base_model import BaseModelBackend
from oasis.social_agent.agent import SocialAgent
from oasis.social_agent.agent_graph import AgentGraph
from oasis.social_platform.channel import Channel
from oasis.social_platform.config.user import UserInfo
from oasis.social_platform.typing import ActionType

from .oasis_trading_tool import OrderCollector, make_freeform_trading_tool, make_submit_order_tool
from .persona import Persona


log = logging.getLogger(__name__)


# set_round_context / clear_round_context live in round_context.py as
# dependency-free helpers so lightweight callers (test suites, external
# tools) can import them without pulling in camel / oasis at module load
# time. Re-exported here for back-compat with existing imports.
from .round_context import clear_round_context, set_round_context


# Event types whose bearish trajectory is typically **permanent** (basic
# fundamentals have changed), so the usual "price fell → expect bounce"
# mean-reversion anchor does not apply. Backtest v2 (analysis/backtest_monthly_v1.md
# §3.1) exposed that the symmetric anchor was pulling 药明康德 back from
# -17% to +4% on an event whose real outcome was -46.5%.
_BEAR_PERMANENT_EVENT_TYPES: frozenset[str] = frozenset({
    "regulatory",         # BIOSECURE-style bans, CSRC 立案, 强制退市
    "lawsuit",            # fraud litigation, class actions
    "shareholder_action", # controlling-shareholder mass offloading
    "geopolitical",       # sanctions, export controls
})

# Analogously, events whose bullish move is typically sustained by a
# durable structural change (policy re-rating, supply re-pricing) should
# not get "price rose → expect pullback" caution at every round.
_BULL_PERMANENT_EVENT_TYPES: frozenset[str] = frozenset({
    "policy",             # 924-style monetary + fiscal combos, MPC cuts
    "supply_disruption",  # commodity super-cycles
    "demand_shock",       # structural demand step-ups
})

# Threshold above which the mean-reversion caution kicks in. The v2
# backtest showed ±3% was far too tight — a single round's rally easily
# crossed it and started injecting "回调风险" on every subsequent round
# of a legitimate uptrend. ±15% is still inside the first-month band of
# most real events but rules out routine noise.
_REVERSION_THRESHOLD_PCT: float = 15.0


def update_conviction_context(
    agent: "SocialAgent",
    persona_id: str,
    last_actions: dict[str, dict],
    cumulative_delta_pct: float = 0.0,
    *,
    current_price: float = 0.0,
    initial_price: float = 0.0,
    event_type: str = "",
) -> None:
    """Inject price-anchored market context as per-round user-instruction text.

    Instead of emphasising "what you did last round" (which creates momentum
    echo), this frames decisions in risk/reward terms: "where is the price
    relative to the event". Writes into ``agent._ssflow_round_context`` so the
    patched action loop picks it up on the next step — the old profile-mutation
    path was broken because CAMEL snapshots the system message at init time.

    Event-type conditioning: mean-reversion anchoring is suppressed for
    permanent-directional events. Regulatory bans / sanctions / fraud
    lawsuits produce secular bear trajectories; policy super-cycles
    produce secular bulls. Injecting "price too low → bounce likely" on
    a secular bear silently kills v1's reach on 药明康德-class events.

    R0 handling: when the persona has no last_action yet (R0), we still
    inject the event-fundamentals block so agents get the directional
    prior immediately. Before this was added, R0 agents drifted with
    feed sentiment and set the tone for the whole sim.
    """
    la = last_actions.get(persona_id, {})
    cum_pct = cumulative_delta_pct * 100
    etype = (event_type or "").lower()
    is_bear_permanent = etype in _BEAR_PERMANENT_EVENT_TYPES
    is_bull_permanent = etype in _BULL_PERMANENT_EVENT_TYPES

    if initial_price > 0 and current_price > 0:
        if cum_pct > _REVERSION_THRESHOLD_PCT:
            if is_bull_permanent:
                price_comment = (
                    f"  当前价格已较事件前上涨 {cum_pct:+.1f}%. "
                    f"这是政策/供给结构性事件驱动的长期 re-rating, "
                    f"不应机械假设短期均值回归 — "
                    f"关注基本面是否仍在验证原始逻辑.\n"
                )
            else:
                price_comment = (
                    f"  当前价格已较事件前上涨 {cum_pct:+.1f}%. "
                    f"价格越高, 继续上涨的空间可能越小, 回调的风险越大.\n"
                )
        elif cum_pct < -_REVERSION_THRESHOLD_PCT:
            if is_bear_permanent:
                price_comment = (
                    f"  当前价格已较事件前下跌 {cum_pct:+.1f}%. "
                    f"这是监管/制裁/基本面级事件, 下跌反映永久性风险重定价, "
                    f"不应机械假设短期均值回归 — "
                    f"关注风险是否出清.\n"
                )
            else:
                price_comment = (
                    f"  当前价格已较事件前下跌 {cum_pct:+.1f}%. "
                    f"价格越低, 继续下跌的空间可能越小, 反弹的可能性越大.\n"
                )
        else:
            price_comment = (
                f"  当前价格较事件前变动 {cum_pct:+.1f}%, 仍在合理波动范围内.\n"
            )
    else:
        price_comment = ""

    side_zh = {"buy": "买入", "sell": "卖出", "hold": "观望"}.get(
        str(la.get("side", "")), str(la.get("side", "") or "尚无上一轮")
    )
    round_label = f"R{la.get('round_idx', -1) + 1}" if la else "R0 (首轮)"

    # Event fundamental direction — explicit prior for agents
    # so policy-bull and regulatory-bear events steer trader flow
    # instead of drifting with feed sentiment. Before this line was
    # added (2026-04-12), the ``round_sentiment_shift`` severity prior
    # only affected Kyle's raw delta via the additive bias — agents'
    # own decisions were still anchored on the OASIS feed, which
    # skewed bearish on every event type. Result: policy rallies
    # (CATL / 东财) got stuck in doom loop because agents saw the
    # event text once at init and then followed social noise.
    event_fundamentals = ""
    if is_bull_permanent:
        event_fundamentals = (
            f"# 事件基本面 / Event Fundamentals\n"
            f"  类型: {etype} — 结构性利好, 市场应持续消化为上行行情.\n"
            f"  历史先例: 政策组合拳/供给冲击/需求爆发类事件, 月度级别\n"
            f"    典型涨幅 +20~+80%, 领涨标的弹性更大.\n"
            f"  预期方向: 上涨 (做空或过度看空会被轧空).\n"
            f"  当前如果你在观望, 考虑是不是错过了入场窗口.\n"
        )
    elif is_bear_permanent:
        event_fundamentals = (
            f"# 事件基本面 / Event Fundamentals\n"
            f"  类型: {etype} — 结构性利空, 基本面被永久重定价.\n"
            f"  历史先例: 监管打击/制裁/欺诈类事件, 月度级别典型跌幅\n"
            f"    -30~-60%, 受冲击最大的标的跌更深.\n"
            f"  预期方向: 下跌 (抄底或赌反弹会继续亏损).\n"
            f"  长期持有人应评估止损, 不要试图接飞刀.\n"
        )
    elif etype == "earnings":
        event_fundamentals = (
            f"# 事件基本面 / Event Fundamentals\n"
            f"  类型: 业绩事件 — 基于市场对未来盈利预期的重估.\n"
            f"  解读: 观察业绩 vs 一致预期的差距 + 管理层指引 + "
            f"渠道反馈.\n"
            f"  预期方向: 取决于 miss / beat 幅度, 读 event_text 判断.\n"
        )

    conviction_text = (
        f"{event_fundamentals}"
        f"# 市场状态 / Market State ({round_label})\n"
        f"  事件前价格: {initial_price:.2f}\n"
        f"  当前价格: {current_price:.2f} (事件后 {cum_pct:+.1f}%)\n"
        f"{price_comment}"
        f"  你上一轮的方向: {side_zh}\n"
        f"  请基于当前价位独立判断本轮方向. 不同价位意味着不同的风险收益比."
    )
    set_round_context(agent, conviction_ctx=conviction_text)


def _patch_perform_action(agent: "SocialAgent", *, is_trader: bool = False) -> None:
    """Monkey-patch SocialAgent.perform_action_by_llm to process ALL tool calls.

    The stock OASIS implementation has a bug: it returns after the FIRST
    tool call in the response (line 152: `return response` inside a for
    loop). This means if the LLM returns [repost, submit_order_distribution]
    in one response, only `repost` gets executed and the trading tool call
    is silently dropped.

    This patch iterates over ALL tool calls and logs non-social ones
    (our custom trading tools) so the OrderCollector captures them.
    """
    from camel.messages import BaseMessage
    from oasis.social_platform.typing import ActionType

    ALL_SOCIAL = {a.value for a in ActionType}
    original_env = agent.env  # the EnvAction bound to this agent

    async def patched_perform_action_by_llm():
        env_prompt = await original_env.to_text_prompt()
        # Per-round context is set by oasis_engine via set_round_context() and
        # prepended to the user instruction. We cannot rely on user_info.profile
        # updates propagating: OASIS/CAMEL bakes the system message from profile
        # only at agent init time, so subsequent profile mutations never reach
        # the LLM. Injecting into the user instruction is a round-scoped, always-
        # fresh alternative that avoids rewriting CAMEL internals.
        round_ctx = getattr(agent, "_ssflow_round_context", None) or {}
        time_ctx = round_ctx.get("time_ctx", "")
        conviction_ctx = round_ctx.get("conviction_ctx", "")
        pub_effects_ctx = round_ctx.get("pub_effects_ctx", "")
        terminal_risk_ctx = round_ctx.get("terminal_risk_ctx", "")
        extra_header = ""
        if any((time_ctx, conviction_ctx, pub_effects_ctx, terminal_risk_ctx)):
            extra_header = (
                f"{terminal_risk_ctx}\n{time_ctx}\n{conviction_ctx}\n{pub_effects_ctx}\n"
            ).strip("\n") + "\n\n"

        if is_trader:
            instruction = (
                f"{extra_header}"
                f"你需要做两件事:\n"
                f"1. **首先必须**调用交易工具提交你的交易决策 (submit_trading_decision)\n"
                f"2. 然后做一个社交动作（发帖/转发/点赞/评论中选一个）\n\n"
                f"你的社交平台环境: {env_prompt}"
            )
        else:
            instruction = (
                f"{extra_header}"
                f"Please perform social media actions after observing the "
                f"platform environments. Notice that don't limit your "
                f"actions for example to just like the posts. "
                f"Here is your social media environment: {env_prompt}")
        user_msg = BaseMessage.make_user_message(
            role_name="User", content=instruction,
        )
        try:
            response = await agent.astep(user_msg)
            tool_calls = response.info.get("tool_calls", [])
            tool_names = [tc.tool_name for tc in tool_calls]
            trading_tools = {"submit_order_distribution", "submit_trading_decision"}
            has_trade = bool(trading_tools & set(tool_names))

            # Log agent decision for observability
            if is_trader:
                log.info(
                    "Agent %d (%s) tools=[%s] trade=%s",
                    agent.social_agent_id,
                    agent.user_info.user_name,
                    ", ".join(tool_names),
                    has_trade,
                )
                if not has_trade:
                    # Retry: the LLM missed the trading tool call
                    resp_text = getattr(response, "content", "") or ""
                    log.warning(
                        "Agent %d (%s) NO TRADE, retrying. tools=[%s] resp=%.200s",
                        agent.social_agent_id,
                        agent.user_info.user_name,
                        ", ".join(tool_names),
                        resp_text,
                    )
                    try:
                        retry_msg = BaseMessage.make_user_message(
                            role_name="User",
                            content=(
                                "你忘了调用 submit_trading_decision 工具. "
                                "请立即调用, 传入 side/quantity_pct/rationale."
                            ),
                        )
                        retry_resp = await agent.astep(retry_msg)
                        retry_calls = retry_resp.info.get("tool_calls", [])
                        retry_names = [tc.tool_name for tc in retry_calls]
                        if trading_tools & set(retry_names):
                            log.info(
                                "Agent %d trade recovered on retry",
                                agent.social_agent_id,
                            )
                            response = retry_resp
                            has_trade = True
                    except Exception:
                        pass  # retry failed, fall through to hold fallback
                for tc in tool_calls:
                    if tc.tool_name in trading_tools:
                        log.info(
                            "Agent %d trade: %s args=%s",
                            agent.social_agent_id,
                            tc.tool_name,
                            str(tc.args)[:300],
                        )

            return response
        except Exception as e:
            log.warning("Agent %d error in patched perform: %s", agent.social_agent_id, e)
            return e

    agent.perform_action_by_llm = patched_perform_action_by_llm


# Stable id for the synthetic market-event broadcaster agent. The engine
# posts price updates as this agent so they propagate through the standard
# OASIS feed mechanism.
MARKET_AGENT_ID_NAME = "__market__"


# Action sets per entity_role.
TRADER_ACTIONS: list[ActionType] = [
    ActionType.CREATE_POST,
    ActionType.LIKE_POST,
    ActionType.REPOST,
    ActionType.QUOTE_POST,
    ActionType.CREATE_COMMENT,
    ActionType.LIKE_COMMENT,
    ActionType.FOLLOW,
    ActionType.SEARCH_POSTS,
    ActionType.REFRESH,
    ActionType.DO_NOTHING,
]

# Pure-info entities don't follow at runtime (their follow list is static
# from YAML) and don't comment in the same way traders do. They mainly post
# and amplify (repost) other publications.
INFO_ACTIONS: list[ActionType] = [
    ActionType.CREATE_POST,
    ActionType.REPOST,
    ActionType.QUOTE_POST,
    ActionType.LIKE_POST,
    ActionType.DO_NOTHING,
]

# Synthetic market broadcaster — only ever does CREATE_POST (via ManualAction).
MARKET_AGENT_ACTIONS: list[ActionType] = [ActionType.CREATE_POST]


def _actions_for(persona: Persona) -> list[ActionType]:
    """Pick the action set for a persona based on its entity_role."""
    if persona.entity_role == "trader":
        return list(TRADER_ACTIONS)
    return list(INFO_ACTIONS)


def _user_info_for(
    persona: Persona,
    *,
    use_freeform_trading: bool = False,
    instrument_universe: "InstrumentUniverse | None" = None,
    event: "Event | None" = None,
) -> UserInfo:
    """Build a CAMEL/OASIS UserInfo from our Persona schema.

    Uses recsys_type="twitter" so OASIS's `to_twitter_system_message` is used
    (which only needs `user_profile`, not the demographic fields the Reddit
    template requires).

    For TRADER personas (`sandbox is not None`), the profile text includes
    explicit instructions to call `submit_order_distribution` every round.
    This is load-bearing: OASIS's hardcoded user prompt (in
    `agent.py:perform_action_by_llm`) says "perform social media actions",
    which biases the LLM away from the trading tool. The trader-specific
    profile text counter-balances this by naming the tool explicitly and
    telling the LLM it's **required** every round.

    `user_name` becomes the persona id (must be unique within the sim db).
    `name` is the human-readable archetype + display_name.
    `description` becomes the bio = voice_prompt (truncated).
    `profile.other_info.user_profile` is the longer character description
    used by the system message builder.
    """
    profile_block = (
        f"角色: {persona.archetype} ({persona.id})\n"
        f"画像: {persona.display_name}\n"
        f"决策模式: {persona.decision_mode}\n"
        f"角色定位: {persona.role}\n"
        f"\n{persona.voice_prompt}"
    )
    if persona.biases:
        bias_lines = "\n".join(
            f"  - {k}: {v}" for k, v in persona.biases.items()
        )
        profile_block += f"\n\n行为偏差:\n{bias_lines}"

    # Inject the market event context so every agent (trader + info) knows
    # what event triggered the simulation — not just from the social feed.
    if event is not None:
        # Pull the event-subject's price + currency from the universe.
        subject_price: float | None = None
        subject_currency = "CNY"
        if instrument_universe is not None and instrument_universe.instruments:
            subject = instrument_universe.get(
                instrument_universe.event_subject_ticker
            )
            if subject is not None:
                subject_price = subject.current_price
                subject_currency = subject.price_currency
        sym = {"CNY": "¥", "USD": "$", "EUR": "€", "JPY": "¥",
               "HKD": "HK$", "BTC": "₿"}.get(subject_currency, "$")
        price_line = (
            f"  当前价格: {sym}{subject_price:.2f}\n"
            if subject_price is not None else ""
        )
        profile_block += (
            f"\n\n# 当前市场事件\n"
            f"  标的: {event.instrument or event.ticker} ({event.ticker})\n"
            f"  事件类型: {event.event_type}\n"
            f"  事件日期: {event.event_date}\n"
            f"{price_line}"
            f"\n{event.event_text.strip()[:500]}\n"
        )

        # Event-type historical precedents. Rendered BEFORE prior_consensus
        # so the LLM reads the directional frame first — recency bias in
        # most LLMs weights the last-read block higher. Previously this
        # block sat after prior_consensus, which meant CATL agents saw
        # "锂电产能过剩担忧" (bearish) as their last contextual anchor
        # and skewed sells against the policy catalyst. Moving the
        # precedents before prior_consensus flips the anchor order:
        # agents see "政策是中国资本市场最强的正面催化剂" first, then
        # "市场此前担忧..." as secondary context.
        _etype = (event.event_type or "").lower()
        if _etype in _BULL_PERMANENT_EVENT_TYPES:
            profile_block += (
                "\n# 历史先例 / Historical Precedents — 政策/供给结构性事件\n"
                "  1. 政策组合拳 (降准+降息+证监会维稳+回购再贷款) "
                "是中国资本市场最强的正面催化剂之一.\n"
                "  2. 2015年7月、2018年末、2020年3月的类似政策环境下, "
                "大盘都在 2-4 个月内 +20~+50%; 券商/新能源/成长股弹性翻倍.\n"
                "  3. 先入者获利最大, 观望者错过入场窗口, "
                "事后解释永远是\"我当时怕XX\".\n"
                "  4. 任何关于\"结构性担忧\"(产能过剩/估值偏高/基本面疑虑) "
                "的叙事在政策级利好面前都是次要的 — 真金白银的资金会先买后问.\n"
                "  5. 如果你是多头机构或散户追涨者: 这是典型的抢筹窗口.\n"
            )
        elif _etype in _BEAR_PERMANENT_EVENT_TYPES:
            profile_block += (
                "\n# 历史先例 / Historical Precedents — 监管/制裁结构性事件\n"
                "  1. 监管打击/制裁/欺诈类事件的第一波下跌往往只是开始, "
                "后续几个月持续有机构被动抛售.\n"
                "  2. 2021年教培行业整治、2018年CFIUS/外资准入收紧、"
                "2020年瑞幸造假类事件, 受冲击标的 3-6 个月跌 -40~-70%.\n"
                "  3. 试图抄底 \"超跌\" 的散户通常继续承受二次下跌, "
                "机构止损会在 -20% 和 -30% 两次加速抛盘.\n"
                "  4. 任何关于\"已经跌够了 / 价格反映充分\"的论据在结构性 "
                "利空面前都需要警惕 — 基本面定价还在持续重估.\n"
                "  5. 如果你是多头持有人: 这是典型的止损/减仓窗口.\n"
            )

        if event.prior_consensus and event.prior_consensus.strip():
            profile_block += (
                f"\n市场此前预期: {event.prior_consensus.strip()[:300]}\n"
            )

    # Trader-specific instructions: counter-balance OASIS's "perform social
    # media actions" user prompt so the LLM remembers to also call the
    # trading tool.
    if persona.sandbox is not None:
        if use_freeform_trading:
            # Free-form mode: LLM decides exact side + quantity_pct
            examples = []
            for a in persona.sandbox.action_space:
                side = a.get("side", "none")
                if side == "none":
                    examples.append("hold (观望)")
                else:
                    frac = a.get("fraction", 0)
                    pool_label = "现金" if a.get("pool") == "cash" else "持仓"
                    examples.append(f"{side} {frac:.0%} of {pool_label}")
            examples_str = ", ".join(examples)
            profile_block += (
                f"\n\n"
                f"# 你是一个交易员 (trader)\n"
                f"除了社交动作, 你**每一轮都必须**调用 `submit_trading_decision` 工具.\n"
                f"你可以自由决定：\n"
                f"  - side: \"buy\"(买入) / \"sell\"(卖出) / \"hold\"(观望)\n"
                f"  - quantity_pct: 0.0 到 1.0 之间的任意数字, 表示动用多少比例\n"
                f"    (买入 = 占可用现金的比例, 卖出 = 占持仓的比例)\n"
                f"  - rationale: 50-150 字中文解释, 引用你处境里的具体数据\n"
                f"\n"
                f"参考（不限于此）: {examples_str}\n"
                f"\n"
                f"基于你当前的处境和 feed 内容, 自由决定具体数字.\n"
                f"**不要不调用这个工具**. 不调用会被视为系统故障.\n"
            )
            if instrument_universe is not None:
                tickers_str = "、".join(
                    f"\"{t}\"" for t in instrument_universe.tickers[:3]
                )
                profile_block += (
                    f"\n"
                    f"# 可交易标的\n"
                    f"{instrument_universe.prompt_summary()}\n"
                    f"\n"
                    f"交易时**必须**在 instrument 参数中指定目标代码 "
                    f"(如 {tickers_str}).\n"
                    f"不指定标的的交易指令将被视为观望.\n"
                    f"你可以一轮内对不同标的分别下单.\n"
                )
                # Append per-instrument market context (holdings, margin)
                ctx_lines = []
                for inst in instrument_universe.instruments:
                    parts = []
                    if inst.holdings_by_persona and persona.id in inst.holdings_by_persona:
                        parts.append(f"你这类持仓占比约{inst.holdings_by_persona[persona.id]:.1f}%")
                    if inst.margin_long_balance:
                        parts.append(f"融资余额{inst.margin_long_balance/1e8:.0f}亿")
                    if parts:
                        ctx_lines.append(f"  {inst.ticker}: {', '.join(parts)}")
                if ctx_lines:
                    profile_block += (
                        f"\n# 市场微观结构\n" + "\n".join(ctx_lines) + "\n"
                    )
        else:
            # Legacy fixed-action mode
            action_names = [a["name"] for a in persona.sandbox.action_space]
            hold_name = next(
                (a["name"] for a in persona.sandbox.action_space
                 if a.get("side") == "none"),
                action_names[0],
            )
            profile_block += (
                f"\n\n"
                f"# 你是一个交易员 (trader)\n"
                f"除了社交动作 (发帖/点赞/转发/关注), 你**每一轮都必须**调用\n"
                f"`submit_order_distribution` 工具, 基于你在 feed 里看到的内容\n"
                f"给出你这一类参与者的动作概率分布.\n"
                f"\n"
                f"这一类的合法动作: {', '.join(action_names)}\n"
                f"\n"
                f"action_distribution 示例 (分布要反映 class 内真实的行为分歧):\n"
                f'  {{"{action_names[0]}": 0.3, "{action_names[-1]}": 0.7}}\n'
                f"\n"
                f"如果这一轮这一类参与者倾向观望, 也要调用工具并传一个\n"
                f"以 `{hold_name}` 为主的分布 (例: {{\"{hold_name}\": 1.0}}).\n"
                f"**不要不调用这个工具**. 不调用的结果会被视为系统故障.\n"
                f"\n"
                f"rationale 字段用 50-150 字中文解释为什么, 要引用 feed 里看到的\n"
                f"具体内容 (哪条新闻 / 哪个分析师 / 哪个 KOL 的帖子).\n"
            )

    # ── create_rule instructions (available to all agents with action_collector) ──
    if persona.sandbox is not None:
        profile_block += (
            f"\n"
            f"# 自动规则工具 (create_rule)\n"
            f"你可以调用 `create_rule` 为自己设置自动执行的风控规则.\n"
            f"规则在每轮自动检查, 条件满足时自动执行动作.\n"
            f"\n"
            f"适用场景:\n"
            f"  - 止损: create_rule(name=\"止损\", trigger=\"price_change_pct < -15.0\",\n"
            f"          action_type=\"trade\", side=\"sell\", quantity_pct=0.5)\n"
            f"  - 止盈: create_rule(name=\"止盈\", trigger=\"price_change_pct > 20.0\",\n"
            f"          action_type=\"trade\", side=\"sell\", quantity_pct=1.0)\n"
            f"  - 仓位控制: create_rule(name=\"减仓\", trigger=\"avg_position_pct > 0.8\",\n"
            f"          action_type=\"trade\", side=\"sell\", quantity_pct=0.3)\n"
            f"\n"
            f"可用变量: avg_position_pct, price_change_pct, avg_cash_pct, total_nav\n"
            f"不要每轮都创建规则 — 只在你判断需要风控时使用.\n"
        )

    return UserInfo(
        user_name=persona.id,
        name=f"{persona.archetype} ({persona.display_name})",
        description=persona.voice_prompt[:200],
        profile={
            "nodes": [],
            "edges": [],
            "other_info": {
                "user_profile": profile_block,
            },
        },
        recsys_type="twitter",
    )


def _market_agent_user_info() -> UserInfo:
    """Build a UserInfo for the synthetic market-event broadcaster."""
    return UserInfo(
        user_name=MARKET_AGENT_ID_NAME,
        name="Market Event Wire",
        description=(
            "Synthetic agent that broadcasts price updates and other market "
            "events as posts so all OASIS agents observe them through their feed."
        ),
        profile={
            "nodes": [],
            "edges": [],
            "other_info": {
                "user_profile": (
                    "I am the market-event broadcaster. I post objective "
                    "price-update headlines after each round of trading. "
                    "I do not have opinions, biases, or recommendations."
                ),
            },
        },
        recsys_type="twitter",
    )


def build_agent_graph(
    personas: list[Persona],
    channel: Channel,
    *,
    model: Optional[BaseModelBackend] = None,
    order_collector: OrderCollector | None = None,
    use_freeform_trading: bool = False,
    instrument_universe: "InstrumentUniverse | None" = None,
    event: "Event | None" = None,
    action_collector: "ActionCollector | None" = None,
) -> tuple[AgentGraph, dict[str, int]]:
    """Build an OASIS `AgentGraph` from our persona YAML.

    Args:
        personas: list of v3 Personas (mix of traders and info entities)
        channel: OASIS Channel that the platform layer uses to talk to agents
        model: the CAMEL model backend each agent calls. Defaults to a fresh
            `SsFishCamelModel` (lazy-imported here to keep this module
            framework-side only).
        order_collector: if provided, every TRADER persona gets a
            `submit_order_distribution` tool bound to this collector. This is
            the Phase II unified-decision path — traders make one LLM call that
            can produce both social actions (via OASIS native tools) AND a
            trading distribution (via this custom tool), from a single
            coherent decision. If `None`, no trading tool is attached and
            traders only see the social actions (Phase I behavior).

    Returns:
        (graph, persona_id_to_oasis_id):
          - graph: the AgentGraph with all personas + the synthetic __market__
            agent added as nodes, and follow edges wired
          - persona_id_to_oasis_id: dict mapping persona.id → integer OASIS id

    Raises:
        ValueError: if `personas` is empty.
    """
    if not personas:
        raise ValueError("build_agent_graph requires at least one persona")

    if model is None:
        from .oasis_lm import build_default_lm
        model = build_default_lm()

    graph = AgentGraph()
    id_map: dict[str, int] = {}

    # ── Step 1: create all real persona agents ──
    for idx, persona in enumerate(personas):
        # Phase II: traders get the trading tool wired via closure.
        # When use_freeform_trading is True (Entity Sandbox mode), the LLM
        # gets the free-form tool (side + quantity_pct) instead of the fixed
        # action_space distribution tool.
        extra_tools = []
        if persona.sandbox is not None and order_collector is not None:
            if use_freeform_trading:
                extra_tools.append(make_freeform_trading_tool(persona, order_collector))
            else:
                extra_tools.append(make_submit_order_tool(persona, order_collector))

        # Non-trader agents: wire domain-specific action tools
        if persona.sandbox is None and action_collector is not None:
            from .action_tools import tool_for_role
            domain_tool = tool_for_role(persona, action_collector)
            if domain_tool is not None:
                extra_tools.append(domain_tool)

        # All agents with an action_collector can create dynamic rules
        if action_collector is not None:
            from .action_tools import make_create_policy_tool
            extra_tools.append(make_create_policy_tool(persona, action_collector))

        # Agents with tools need max_iteration >= 2 so CAMEL's ChatAgent
        # does a multi-turn tool-calling loop. Without it the LLM picks
        # ONE tool (usually create_post) and stops.
        max_iter = 2 if extra_tools else 1

        agent = SocialAgent(
            agent_id=idx,
            user_info=_user_info_for(
                persona,
                use_freeform_trading=use_freeform_trading,
                instrument_universe=instrument_universe,
                event=event,
            ),
            channel=channel,
            model=model,
            agent_graph=graph,
            available_actions=_actions_for(persona),
            tools=extra_tools,
            max_iteration=max_iter,
        )
        # Patch: OASIS stock perform_action_by_llm returns after the first
        # tool call, dropping subsequent calls (including our trading tool).
        # Our patch iterates all tool calls so the OrderCollector sees them.
        _patch_perform_action(agent, is_trader=(persona.sandbox is not None))
        graph.add_agent(agent)
        id_map[persona.id] = idx

    # ── Step 2: create the synthetic market broadcaster ──
    market_idx = len(personas)
    market_agent = SocialAgent(
        agent_id=market_idx,
        user_info=_market_agent_user_info(),
        channel=channel,
        model=model,
        agent_graph=graph,
        available_actions=list(MARKET_AGENT_ACTIONS),
    )
    graph.add_agent(market_agent)
    id_map[MARKET_AGENT_ID_NAME] = market_idx

    # ── Step 3: wire follow edges ──
    # Edge direction: follower → followee
    for persona in personas:
        follower_idx = id_map[persona.id]
        followees = _resolve_follows(persona, id_map, personas)
        for followee_idx in followees:
            if followee_idx != follower_idx:  # don't self-follow
                graph.add_edge(follower_idx, followee_idx)

    # ── Step 4: every trader implicitly follows the market broadcaster ──
    for persona in personas:
        if persona.sandbox is not None:  # is a trader
            trader_idx = id_map[persona.id]
            graph.add_edge(trader_idx, market_idx)

    log.info(
        "Built OASIS agent graph: %d personas + 1 market agent, %d follow edges",
        len(personas), graph.get_num_edges(),
    )
    return graph, id_map


def _resolve_follows(
    persona: Persona,
    id_map: dict[str, int],
    all_personas: list[Persona],
) -> set[int]:
    """Resolve a persona's `follows` list to a set of OASIS integer ids.

    Special values:
      - "*"          → every other persona in the pack
      - "__market__" → the synthetic market broadcaster

    Skips unknown ids with a warning (the persona schema validator should
    catch these at load time, but defend in depth).
    """
    out: set[int] = set()
    for ref in persona.follows:
        if ref == "*":
            for p in all_personas:
                out.add(id_map[p.id])
        elif ref == MARKET_AGENT_ID_NAME:
            if MARKET_AGENT_ID_NAME in id_map:
                out.add(id_map[MARKET_AGENT_ID_NAME])
        elif ref in id_map:
            out.add(id_map[ref])
        else:
            log.warning(
                "persona '%s' follows unknown id '%s' — skipping",
                persona.id, ref,
            )
    return out


__all__ = [
    "MARKET_AGENT_ID_NAME",
    "TRADER_ACTIONS",
    "INFO_ACTIONS",
    "MARKET_AGENT_ACTIONS",
    "build_agent_graph",
]
