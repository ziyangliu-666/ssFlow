"""ssFlow — A 股事件舆情推演引擎.

Module layout:
    config        - env-driven settings singleton
    llm_client    - AsyncOpenAI wrapper for yourapi.cn (with cost tracking + retry)
    persona       - Persona dataclass + YAML loader
    event         - Event input schema
    simulation    - Batched multi-round multi-persona orchestration
    aggregation   - Group reactions -> implied price move
    output_filter - Compliance regex blocklist (CRITICAL, load-bearing)
    report        - Markdown report rendering
    scorecard     - SQLite logging for OOD evaluation
"""

__version__ = "0.0.1"
