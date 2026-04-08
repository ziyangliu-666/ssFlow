# ssFish — A 股事件舆情推演引擎

把一个公告/事件甩进去, 让 10 个不同人格的 agent 互相讨论 5 轮, 输出:

1. 群体反应叙事 (谁会怎么想, 为什么)
2. 盲点清单 (你可能漏掉的解读角度)
3. 模拟群体的 implied price move (描述性, 不是建议)

**这不是投资建议工具。** 这是一个事件研究 / 舆情推演工具。所有输出都是描述性的, 严格不含具体的买入卖出建议或目标价。详见 `src/ssfish/output_filter.py` 的合规防火墙。

## Quickstart

```bash
# 1. 配置环境
cp .env.example .env
$EDITOR .env   # 填入 OPENAI_API_KEY (yourapi.cn) 和 SSFISH_PASSWORD

# 2. 安装依赖
uv sync

# 3. 跑测试 (合规过滤器 + persona schema 必须过)
uv run pytest -v

# 4a. 命令行跑一次模拟
echo "比亚迪一季度新能源汽车销量同比 +18%, 毛利率 -2.3pp" > /tmp/event.txt
uv run python scripts/run_one.py \
    --event /tmp/event.txt \
    --ticker 002594 \
    --event-type earnings \
    --event-date 2026-04-09

# 4b. 或者跑 Flask
uv run flask --app api.app run --port 5001
# 然后浏览器打开 http://localhost:5001/
```

## 架构 (Week 0-2 MVP)

```
event input ──▶ simulation (5 rounds × 10 personas) ──▶ aggregation
                       │                                       │
                       └──▶ all reactions stored                ▼
                                                       output_filter (regex 黑词单)
                                                                │
                                                                ▼
                                                       markdown report
                                                                │
                                                                ▼
                                                       SQLite scorecard
```

- **engine**: `src/ssfish/` (15 个模块, 与 web/CLI 完全解耦, 为 Week 5+ 开源抽离做好准备)
- **web**: `api/` (Flask, 单密码 basic auth)
- **personas**: `personas/ashare-v1.yaml` (10 个 hardcoded archetype)
- **tests**: `tests/` (合规过滤器是 launch blocker, 必须 100% 通过)

详细设计文档: `~/.gstack/projects/ssFish/rufus-main-design-20260407-202742.md`
测试计划: `~/.gstack/projects/ssFish/rufus-main-eng-review-test-plan-20260407-210444.md`

## ⚠️ 合规说明

本工具定位为**事件舆情研究工具**, 不提供投资建议。所有生成的内容基于假设性多 agent 模拟,
仅供研究参考, 不构成任何证券投资咨询业务。使用本工具即表示您理解这一点。

详见 `src/ssfish/output_filter.py` — 输出会经过严格的措辞过滤, 任何包含
"建议/买入/卖出/目标价/评级" 等词汇的内容都会被自动拦截。

## License

MIT (Week 6+ 会重新评估, 可能改为非商用 + 持有人保留商业权利)
