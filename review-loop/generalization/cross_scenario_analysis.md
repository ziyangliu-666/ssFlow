# ssFlow Generalizability Test — Cross-Scenario Analysis

## Test Matrix (8 scenarios, post-fix)

| # | Input | Category | Direction | ΔPrice | Initial | Correct? |
|---|-------|----------|-----------|--------|---------|----------|
| 1 | 比亚迪Q1财报超预期 | 主板-业绩 | 利好 | +3.48% | ¥104.25 | ✓ |
| 2 | 茅台Q1营收首降 | 主板-业绩 | 利空 | -7.21% | ¥1443.31 | ✓ |
| 3 | 央行下调MLF利率25bp | 宏观-降息 | 利好 | +12.80% | ¥11.07 | ✓方向，价格异常 |
| 4 | 互联网平台反垄断罚款 | 互联网-监管 | 利空 | -25.37% | ¥1.35 | ✓方向，价格完全错误 |
| 5 | 神华吸收合并国电 | 央企-重组 | 利好 | +16.30% | ¥46.32 | ✓(修复后) |
| 6 | 芯瞳半导体IPO首日 | 创业板-IPO | 利好 | +14.05% | ¥100.00 | ✓方向，fallback价格 |
| 7 | 百济神州GLP-1临床 | 科创板-药 | 分歧 | +3.58% | ¥34.93 | ✓ |
| 8 | 科大讯飞星火4.0 | AI概念 | 利好 | +12.67% | ¥47.78 | ✓ |

**Direction accuracy**: 8/8 correct (100%), up from 7/8 pre-fix
**Price extraction**: 5/8 reasonable, 3/8 anomalous

## Cross-scenario patterns

### Pattern 1: Short funds sell regardless of event direction
In ALL 8 scenarios, 事件驱动做空基金 and 全球宏观对冲基金 produced net sell flow:
- 利好 events (5 cases): still sold ¥1.42-7.50億 each
- 利空 events (2 cases): sold ¥7.05億 each (expected)
- 分歧 event (1 case): one sold ¥0.62億 (muted, reasonable)

The voice_prompt fix changed their RATIONALE (from "做空获利" to "控制风险") but not their ACTION. The LLM always picks the sell action.

### Pattern 2: Recurring "cap" flow amounts
These exact amounts appear across multiple scenarios:
- ¥2.66億 (most common, appears in 3+ scenarios)
- ¥1.42億
- ¥7.05億
- ¥7.50億

These are per-class ADV flow cap amounts, not organic flow decisions. A real fund manager would never see the same exact order size across completely different stocks and events.

### Pattern 3: P&L makes no physical sense
Short sellers PROFIT in bull markets:
- BYD earnings beat (+3.48%): event_driven_short_fund P&L = +¥90.77M
- 神华 M&A (+16.30%): event_driven_short_fund P&L = +¥669.77M
- 讯飞 AI (+12.67%): event_driven_short_fund P&L = +¥726.73M

Short sellers should LOSE money when prices go up. This suggests the P&L calculation is wrong (possibly treating sell as "selling existing holdings for profit" rather than "shorting").

### Pattern 4: Passive holders dominate P&L table
In every single scenario, major_individual_holder (3 agents) generates 60-70% of total P&L. This is unrealized gains on their massive existing positions, not trading skill.

### Pattern 5: Price extraction fragility
3/8 cases produced wrong initial prices:
- Macro events (央行降息) → extractor picks wrong instrument (¥11.07 suggests an ETF, not an index)
- Vague inputs ("某头部互联网平台") → no real ticker match (¥1.35)
- Fictional companies → fallback ¥100

## Verdict

方向对了(8/8), 但细节不经推敲:
1. 做空基金的决策和 prompt 改了但行为没变 → 需要 action_space 层面修
2. 流量全是 cap 值 → 需要让 LLM 决定交易比例, 不只是方向
3. P&L 表对做空者的计算有系统性错误
4. 价格提取对模糊输入脆弱
