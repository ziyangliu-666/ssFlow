# Round 20 Analysis — Why institutional participants are "stage props"

## Problem
Codex R19 review: "18类参与者中，真正定价的只有3股力量（散户追涨、做空基金、私募多空）。
量化、ETF AP、北上、险资、国家队全程观望——全是布景板。"

## 5 Whys

**问题**: 只有 3/14 trader 类型实际交易

**Why 1**: 机构参与者每轮都选 "hold"
→ 直接原因：LLM 读了 voice_prompt 后判断"这个事件不触发我的交易条件"

**Why 2**: voice_prompt 明确说"突发事件下不动"
→ etf_passive: "突发事件 → 你不主动反应"
→ quant_hft: "不读新闻, 不看研报"
→ insurance_pension: "突发事件下你几乎不动" + reaction_lag=[3,4,4,5,5,5] → 3轮模拟中物理上不可能交易
→ northbound: 只提了 DXY/MSCI 触发器，没提央行政策

**Why 3**: prompt 没有区分事件量级——"降准50bp"被当成跟"某券商出了研报"一样的突发事件
→ 降准 50bp 是 A 股最强正面催化剂之一，险资/北上/量化/ETF 都有明确的响应逻辑
→ 但 voice_prompt 写的是"一般情况下不动"的 blanket rule

**Why 4**: persona 设计把"反应慢"等同于"不反应"
→ 险资反应季级（对），但央行降准不是日内噪声——它重定了整个利率曲线
→ ETF AP 不判断方向（对），但客户的申赎行为在大事件下确定性暴增

**Why 5**: voice_prompt 缺少"事件-触发器"连接
→ 没有告诉 LLM：降准 = 负债端再平衡 = 你要动
→ 没有告诉 LLM：大事件 = 客户申赎暴增 = ETF 必须买卖
→ 没有告诉 LLM：NLP 情绪极端分 = 量化信号必然触发

## 根因层级判定

Why 3-4 → **L2（LLM 提示词层）**

## 本轮修复

修改 3 个能交易但选择观望的 trader 的 voice_prompt：
1. **etf_passive**: 明确"大事件 = 客户大量申赎 = 你必须对应买卖"
2. **quant_hft**: 明确"重大宏观事件 = NLP 情绪极分 = 信号必触发"
3. **northbound_qfii**: 明确"央行政策 = risk-on/off 信号 = 你会调仓"

不改 insurance_pension（reaction_lag 物理阻止其交易，需要 L1 另修）。

## 未来轮次 TODO
- L1: 降低 insurance_pension 的 reaction_lag（至少让部分 agent 在 R1-R2 可参与）
- L2: 修复 insurance_pension 的 voice_prompt（同时做）
