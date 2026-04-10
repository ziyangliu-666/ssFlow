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
        if is_trader:
            instruction = (
                f"观察以下社交平台信息后，你需要做两件事:\n"
                f"1. 先做一个社交动作（发帖/转发/点赞/评论中选一个）\n"
                f"2. **必须**调用交易工具提交你这一类参与者的交易决策\n\n"
                f"你的社交平台环境: {env_prompt}"
            )
        else:
            instruction = (
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
                    # Log why the trader didn't trade — this is the key diagnostic
                    resp_text = getattr(response, "content", "") or ""
                    log.warning(
                        "Agent %d (%s) NO TRADE. tools=[%s] resp_preview=%.200s",
                        agent.social_agent_id,
                        agent.user_info.user_name,
                        ", ".join(tool_names),
                        resp_text,
                    )
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

        # Traders need max_iteration >= 2 so CAMEL's ChatAgent does a
        # multi-turn tool-calling loop. With max_iteration=1 (OASIS default)
        # the LLM gets exactly one shot, and it tends to pick ONE tool —
        # usually `create_post` — and stop. With max_iteration=2 the agent
        # gets a second turn where we can more reliably reach the
        # submit_order_distribution tool. Info entities stay at 1 since
        # they only have social actions.
        max_iter = 2 if persona.sandbox is not None else 1

        agent = SocialAgent(
            agent_id=idx,
            user_info=_user_info_for(
                persona,
                use_freeform_trading=use_freeform_trading,
                instrument_universe=instrument_universe,
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
