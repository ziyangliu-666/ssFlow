"""Settings loader. Reads from .env via pydantic-settings.

Usage:
    from ssfish.config import settings
    print(settings.openai_base_url)
"""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All ssFish runtime configuration. One singleton."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── LLM API (yourapi.cn OpenAI-compatible) ─────────────────────────────
    openai_api_key: SecretStr = Field(..., alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://yourapi.cn/v1", alias="OPENAI_BASE_URL")

    # ── Simulation knobs ───────────────────────────────────────────────────
    default_model: str = Field(default="gpt-4o-mini", alias="SSFISH_DEFAULT_MODEL")
    n_rounds: int = Field(default=5, alias="SSFISH_N_ROUNDS")
    temperature: float = Field(default=0.0, alias="SSFISH_TEMPERATURE")
    seed: int = Field(default=42, alias="SSFISH_SEED")

    # ── Cost guard ─────────────────────────────────────────────────────────
    budget_usd: float = Field(default=5.0, alias="SSFISH_BUDGET_USD")
    cost_ledger_path: str = Field(default="./.cost_ledger.json", alias="SSFISH_COST_LEDGER")

    # ── Local storage ──────────────────────────────────────────────────────
    scorecard_db_path: str = Field(default="./scorecard.db", alias="SSFISH_SCORECARD_DB")

    # ── Flask basic auth ───────────────────────────────────────────────────
    flask_password: SecretStr = Field(default=SecretStr("change-me"), alias="SSFISH_PASSWORD")

    # ── Project paths ──────────────────────────────────────────────────────
    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    @property
    def personas_dir(self) -> Path:
        return self.project_root / "personas"

    @property
    def reports_dir(self) -> Path:
        d = self.project_root / "reports"
        d.mkdir(exist_ok=True)
        return d


# Module-level singleton. Import this everywhere.
try:
    settings = Settings()  # type: ignore[call-arg]
except Exception as exc:  # pragma: no cover - bootstrap failure
    # Allow imports during testing without a real .env (tests use monkeypatch)
    import os

    os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")
    settings = Settings()  # type: ignore[call-arg]
    del exc
