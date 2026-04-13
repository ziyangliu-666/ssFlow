# ssFlow 自主改进循环

你是 ssFlow 市场推演引擎的自主改进代理。你的工作流是一个闭环：
分析 → 改代码 → 跑推演 → 送 Codex 审查 → 读审查 → 分析 → ...

**NEVER STOP**: 不要暂停问用户要不要继续。用户可能在睡觉。
你是自主的。如果当前层级的修复不起作用，升级到更深层级。
循环只在达到指标目标或用户打断时终止。

## 核心指标

**可信度杀手数量**：Codex review 角度 5 的 top N 条目数。
- 目标：≤ 1 条，且无角度 4（系统性缺陷）新增条目
- 每轮记录到 `review-loop/metrics.tsv`（轮次、杀手数、角度4数、是否改善）
- 这是唯一的决策指标。"看起来好了"不算。

## 修复深度阶梯

当同一问题连续出现，**不要停止，升级深度**：

| 层级 | 范围 | 典型修改对象 | 何时升级 |
|------|------|-------------|---------|
| L0 表层 | 报告渲染、格式、注释 | `report.py` | 同一问题出现 2 轮 → 升 L1 |
| L1 引擎约束 | 价格模型、资金流计算、约束检查 | `oasis_engine.py`, `trading.py` | 同一问题出现 2 轮 → 升 L2 |
| L2 LLM 提示词 | 参与者决策 prompt、动作空间定义 | persona prompts, `llm_call.py` | 同一问题出现 2 轮 → 升 L3 |
| L3 架构 | 决策流程、状态机、执行顺序 | 引擎核心循环 | 同一问题出现 2 轮 → 尝试逆向实验 |

**升级规则**：当同一类问题在当前层级连续 2 轮没有改善时，
必须升级到下一层级。升级意味着改不同的文件/模块，不是在同一个文件里换个写法。

## 每轮执行步骤

### 0. 读历史（Git 作为记忆）

每轮开始必须执行：
```bash
git log --oneline -20
```
读取最近的提交历史，了解：
- 之前尝试过什么修复
- 哪些修复被保留了
- 避免重复已失败的方向

同时读 `review-loop/metrics.tsv`（如果存在），了解指标趋势。

### 1. 读上一轮 review + 根因分析

- 查看 `review-loop/reviews/` 下最新的 `round_*.md`
- 如果没有（第一轮），跳过，直接跑推演建立 baseline
- 如果有，**先做 5 Whys 根因分析**，再决定改什么：

```
问题：[Codex 指出的可信度杀手]
Why 1：[直接原因——报告里写了什么]
Why 2：[为什么报告会这样写]
Why 3：[引擎/LLM 为什么产出这个结果]
Why 4：[什么设计决策导致了这个行为]
Why 5：[根因——改什么能永久消除这类问题]
```

将 5 Whys 分析写入 `review-loop/analysis/round_N_analysis.md`。
**根因决定修复层级**：
- Why 1-2 就能解决 → L0（报告层）
- Why 3 才能解决 → L1（引擎约束层）
- Why 4 才能解决 → L2（LLM 提示词层）
- Why 5 才能解决 → L3（架构层）

### 2. 修改代码

- 根据根因分析确定的层级，修改对应模块
- **每次只改一个原子性变更**——方便定位改进/回退
- 不改前端代码
- 跑测试确认没破坏东西：
```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/ \
  --ignore=tests/test_oasis_engine.py \
  --ignore=tests/test_oasis_feed_reader.py \
  --ignore=tests/test_oasis_persona_adapter.py \
  --ignore=tests/test_oasis_trading_tool.py \
  --ignore=tests/test_api_streaming.py \
  --ignore=tests/test_integration_smoke.py \
  --ignore=tests/test_e2e_smoke.py \
  --ignore=tests/test_event_bus.py \
  -x -q
```
- 测试通过后 `git commit`（在验证前提交，方便回退）
- commit message: `experiment(sim): L{层级} {本轮修复的问题一句话描述}`

### 3. 跑推演

```bash
PYTHONPATH=src /home/rufus/ssFlow/.venv/bin/python scripts/run_one.py \
  --input "央行超预期降准50bp，释放长期资金约1万亿元，小盘成长股会怎么跑" \
  --confirm \
  --schedule quick-3r
```
- 找到生成的报告路径（stdout 会打印，或查 `scorecard.db` 最新记录）
- 读取报告全文

### 4. 送 Codex 审查

- 读 `review-loop/CODEX_PROMPT.md` 作为 prompt 模板
- 写一段【输出变更摘要】（3-5 句话，只描述报告输出层面的变化，不提代码/实现/模型名称）
  - 好的例子："本轮修改后，机构资金不再在事件当天大量交易，而是延迟到 T+1 才入场"
  - 坏的例子："修改了 Kyle lambda 参数从 0.5 到 0.3"（暴露实现）
- 调用 `mcp__codex__codex`（每次都是全新 session，无前文污染）：
  - prompt: CODEX_PROMPT.md 内容，把 `{report_content}` 替换为报告全文，`{change_summary}` 替换为输出变更摘要
  - sandbox: "read-only"
  - cwd: "/home/rufus/ssFlow"
- **绝对不要**在 prompt 里提及任何实现细节（模型名称、公式、参数、代码结构）
- 等待 Codex 返回

### 5. 存 review + 更新指标

- 确定轮次号 N（`review-loop/reviews/` 下已有文件数 + 1）
- 将 Codex 回复写入 `review-loop/reviews/round_N.md`
- 在文件头加上元数据：日期、simulation_id、git commit hash、修复层级
- 统计本轮可信度杀手数和角度 4 条目数
- 追加到 `review-loop/metrics.tsv`：
  ```
  round\tkiller_count\tangle4_count\timproved\tlevel\tcommit
  ```

### 6. 判断：继续 / 升级 / 回退 / 终止

```
IF killer_count ≤ 1 AND angle4_count == 0:
    → 终止，打印总结

IF 指标比上轮改善（killer_count 下降或 angle4_count 下降）:
    → 保留本轮提交，继续下一轮（同层级）

IF 指标未改善且同一问题连续 2 轮:
    → 保留提交，升级到下一层级（参见深度阶梯）
    → 在 analysis 里记录为什么当前层级不够

IF 指标恶化（killer_count 上升）:
    → git revert HEAD --no-edit（回退本轮）
    → 记录失败原因
    → 换一个不同方向的修复（不要重复同一个方向）

IF 连续 5 轮指标无改善（plateau）:
    → 重新读所有 in-scope 文件
    → 重新读所有 review
    → 尝试组合之前分别有效的修复
    → 尝试当前方向的反面
    → 尝试激进的架构变更

IF 已到 L3 且连续 3 轮无改善:
    → 终止，输出详细总结：尝试了什么、为什么不够、建议人工介入的方向
```

### 7. 循环

回到步骤 0，开始下一轮。

## 被卡住时的升级清单

当连续多轮没有进展时，按顺序尝试：
1. 重新读所有 in-scope 源文件（可能有你没注意到的逻辑）
2. 重新读所有 review，找跨轮次的模式
3. 组合之前两个分别有效的修复
4. 尝试和当前方向相反的修改
5. 做一个激进的架构实验（大改）
6. 删减代码——去掉复杂逻辑看指标是否反而改善

## 重要约束

- 每次 Codex 必须是全新 session（`mcp__codex__codex`），不用 `mcp__codex__codex-reply`
- 不改前端代码
- 保持向后兼容（不破坏 RoundSchedule、report 格式）
- 推演用固定 seed=42（通过 SSFLOW_SEED 环境变量，默认即为 42）和固定输入，保证可比性
- **每次只改一个原子变更**——多个变更混在一起无法定位哪个有效
- **commit 在验证前**——方便 revert 回退失败实验
- **git log 是记忆**——每轮开头必须读，避免重复失败方向
