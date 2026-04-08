"""Settings loader. Reads from .env via pydantic-settings.

Usage:
    from ssflow.config import settings
    print(settings.openai_base_url)
"""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All ssFlow runtime configuration. One singleton."""

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
    default_model: str = Field(default="gpt-4o-mini", alias="SSFLOW_DEFAULT_MODEL")
    n_rounds: int = Field(default=5, alias="SSFLOW_N_ROUNDS")
    temperature: float = Field(default=0.0, alias="SSFLOW_TEMPERATURE")
    seed: int = Field(default=42, alias="SSFLOW_SEED")

    # ── Cost guard ─────────────────────────────────────────────────────────
    budget_usd: float = Field(default=5.0, alias="SSFLOW_BUDGET_USD")
    cost_ledger_path: str = Field(default="./.cost_ledger.json", alias="SSFLOW_COST_LEDGER")

    # ── Local storage ──────────────────────────────────────────────────────
    scorecard_db_path: str = Field(default="./scorecard.db", alias="SSFLOW_SCORECARD_DB")

    # ── Flask basic auth ───────────────────────────────────────────────────
    flask_password: SecretStr = Field(default=SecretStr("change-me"), alias="SSFLOW_PASSWORD")

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
#
# Fail-fast policy (retrospective finding: the old silent fallback to
# "sk-test-placeholder" was the #1 DX bug — it deferred missing-env-var
# failures to an opaque 401 from yourapi.cn much later in the call chain).
#
# Tests set OPENAI_API_KEY via tests/conftest.py at conftest-import time,
# BEFORE any `import ssflow.config` happens — so tests keep working.
# Real runs need a real .env file or exported env vars.
try:
    settings = Settings()  # type: ignore[call-arg]
except Exception as exc:
    raise RuntimeError(
        "ssFlow could not load configuration. Most likely cause: OPENAI_API_KEY "
        "is not set.\n"
        "\n"
        "To fix:\n"
        "  1. Copy .env.example to .env:   cp .env.example .env\n"
        "  2. Edit .env and add your key from https://yourapi.cn (or any "
        "OpenAI-compatible endpoint).\n"
        "  3. Make sure OPENAI_BASE_URL points to your provider (default: "
        "https://yourapi.cn/v1).\n"
        "\n"
        f"Underlying error: {exc}"
    ) from exc
