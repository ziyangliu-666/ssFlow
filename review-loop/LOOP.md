# ssFlow 自主改进循环

你是 ssFlow 市场推演引擎的自主改进代理。你的工作流是一个闭环：
改代码 → 跑推演 → 送 Codex 审查 → 读审查 → 改代码 → ...

## 每轮执行步骤

### 1. 读上一轮 review
- 查看 `review-loop/reviews/` 下最新的 `round_*.md`
- 如果没有（第一轮），跳过，直接跑推演
- 如果有，提取【最高优先级修复建议】作为本轮目标

### 2. 修改代码
- 根据 review 的最高优先级建议，修改 `src/ssflow/` 下的模拟逻辑
- 只改模拟引擎代码，不改前端
- 跑 `python -m pytest tests/ --ignore=tests/test_oasis_engine.py --ignore=tests/test_oasis_feed_reader.py --ignore=tests/test_oasis_persona_adapter.py --ignore=tests/test_oasis_trading_tool.py --ignore=tests/test_api_streaming.py --ignore=tests/test_integration_smoke.py --ignore=tests/test_e2e_smoke.py --ignore=tests/test_event_bus.py -x -q` 确认没破坏东西
- 如果是第一轮（没有 review），跳过修改，直接跑推演建立 baseline

### 3. 跑推演
```bash
PYTHONPATH=src /home/rufus/miniconda3/envs/ssflow/bin/python scripts/run_one.py \
  --input "央行超预期降准50bp，释放长期资金约1万亿元，小盘成长股会怎么跑" \
  --confirm \
  --schedule quick-3r \
  --seed 42
```
- 找到生成的报告路径（stdout 会打印，或查 `scorecard.db` 最新记录）
- 读取报告全文

### 4. 送 Codex 审查
- 读 `review-loop/CODEX_PROMPT.md` 作为 prompt 模板
- 用 `git diff HEAD~1` 获取本轮代码变更
- 调用 `mcp__codex__codex`（每次都是全新 session，无前文污染）：
  - prompt: CODEX_PROMPT.md 内容 + "\n\n===== 报告全文 =====\n" + 报告内容 + "\n\n===== 本轮代码变更 =====\n" + git diff
  - sandbox: "read-only"
  - cwd: "/home/rufus/ssFlow"
- 等待 Codex 返回

### 5. 存 review
- 确定轮次号 N（`review-loop/reviews/` 下已有文件数 + 1）
- 将 Codex 回复写入 `review-loop/reviews/round_N.md`
- 在文件头加上元数据：日期、simulation_id、git commit hash

### 6. 判断是否继续
- 如果 Codex 的"可信度杀手"数量 ≤ 1 且没有新的角度 4（设计缺陷），可以停止
- 如果连续两轮的最高优先级建议相同（说明改不动了），停止
- 否则继续下一轮

### 7. Commit
- 每轮修改完代码、确认测试通过后，git commit（不 push）
- commit message: `fix(sim): {本轮修复的问题一句话描述}`

## 重要约束
- 每次 Codex 必须是全新 session（`mcp__codex__codex`），不用 `mcp__codex__codex-reply`
- 不改前端代码
- 不改 persona YAML
- 保持向后兼容（不破坏 RoundSchedule、report 格式）
- 推演用固定 seed=42 和固定输入，保证可比性
