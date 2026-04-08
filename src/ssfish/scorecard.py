"""SQLite scorecard for OOD evaluation tracking.

Each row records one simulation: inputs, outputs, model/persona/seed identity
(for reproducibility), and slots for the actual market outcome (filled in
manually after T+5 days for now). The Live Scorecard public page in a future
release renders this table.

Schema design follows premise P5 from the design doc — every entry stores
the prompt+persona+model hashes so reruns can be verified bit-identical.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings


SCHEMA_VERSION = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS simulations (
    simulation_id        TEXT PRIMARY KEY,
    event_ticker         TEXT NOT NULL,
    event_date           TEXT NOT NULL,
    event_type           TEXT,
    event_text_hash      TEXT NOT NULL,
    persona_set_hash     TEXT NOT NULL,
    model_default        TEXT,
    seed                 INTEGER,
    n_personas           INTEGER,
    n_rounds             INTEGER,
    -- Sentiment-mode outputs (NULL when mode='sandbox')
    sentiment_mean       REAL,
    sentiment_std        REAL,
    implied_move_low     REAL,
    implied_move_high    REAL,
    implied_move_confidence REAL,
    blind_spots_json     TEXT,
    full_report_path     TEXT,
    cost_usd             REAL,
    elapsed_seconds      REAL,
    created_at           TEXT NOT NULL,
    -- For T+5 day comparison (manually updated for now)
    actual_first_day_move    REAL,
    actual_first_week_move   REAL,
    diff_recorded_at         TEXT,
    -- Added in schema v2 (retrospective reproducibility fix): capture provider
    -- fingerprints and the actual seed passed to the LLM API. Old rows will
    -- have NULL here; new rows populate these.
    round_fingerprints_json  TEXT,
    llm_seed                 INTEGER,
    -- Added in schema v3 (Phase B sandbox mode): sandbox-specific outputs
    -- and metadata. NULL when mode='sentiment'.
    mode                     TEXT DEFAULT 'sentiment',
    initial_price            REAL,
    final_price              REAL,
    cumulative_delta_pct     REAL,
    price_trajectory_json    TEXT,
    class_pnl_json           TEXT,
    strategic_signals_json   TEXT,
    lambda_used              REAL,
    adv_used                 REAL
);
"""

# Indexes are created AFTER migrations so they can reference v3 columns
# (e.g., `mode`) on existing v2 databases without crashing.
INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_sim_ticker_date ON simulations(event_ticker, event_date);
CREATE INDEX IF NOT EXISTS idx_sim_event_hash ON simulations(event_text_hash);
CREATE INDEX IF NOT EXISTS idx_sim_mode ON simulations(mode);
"""


# Migration helper for existing scorecard.db files that predate v2.
# Called from _connect() on every connection (idempotent).
_V2_MIGRATIONS_SQL = [
    "ALTER TABLE simulations ADD COLUMN round_fingerprints_json TEXT",
    "ALTER TABLE simulations ADD COLUMN llm_seed INTEGER",
]

# v3 adds the sandbox-mode columns. Same idempotent ALTER TABLE pattern.
_V3_MIGRATIONS_SQL = [
    "ALTER TABLE simulations ADD COLUMN mode TEXT DEFAULT 'sentiment'",
    "ALTER TABLE simulations ADD COLUMN initial_price REAL",
    "ALTER TABLE simulations ADD COLUMN final_price REAL",
    "ALTER TABLE simulations ADD COLUMN cumulative_delta_pct REAL",
    "ALTER TABLE simulations ADD COLUMN price_trajectory_json TEXT",
    "ALTER TABLE simulations ADD COLUMN class_pnl_json TEXT",
    "ALTER TABLE simulations ADD COLUMN strategic_signals_json TEXT",
    "ALTER TABLE simulations ADD COLUMN lambda_used REAL",
    "ALTER TABLE simulations ADD COLUMN adv_used REAL",
]


def _idempotent_alter_columns(
    conn: sqlite3.Connection, statements: list[str]
) -> None:
    """Apply a list of `ALTER TABLE simulations ADD COLUMN` statements,
    skipping any whose target column already exists. Used by both v2 and v3
    migrations."""
    cur = conn.execute("PRAGMA table_info(simulations)")
    existing_cols = {row[1] for row in cur.fetchall()}
    for alter_sql in statements:
        col_name = alter_sql.split("ADD COLUMN ")[1].split()[0]
        if col_name not in existing_cols:
            try:
                conn.execute(alter_sql)
            except sqlite3.OperationalError:
                # Column already exists from a concurrent migration; safe to ignore.
                pass


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Idempotently add v2 columns to pre-existing v1 databases."""
    _idempotent_alter_columns(conn, _V2_MIGRATIONS_SQL)


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """Idempotently add v3 sandbox-mode columns to pre-v3 databases."""
    _idempotent_alter_columns(conn, _V3_MIGRATIONS_SQL)


# ─────────────────────── Connection helper ───────────────────────


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection. Creates parent dir + applies schema on demand.

    Runs idempotent v1→v2 column migrations before yielding, so existing
    databases from the initial MVP commit pick up the new reproducibility
    columns without data loss.
    """
    db_path = Path(settings.scorecard_db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA_SQL)
        _migrate_to_v2(conn)
        _migrate_to_v3(conn)
        # Indexes created AFTER migrations so the mode-index can find its column
        conn.executescript(INDEX_SQL)
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Idempotently create the schema. Can be called at app startup."""
    with _connect():
        pass  # _connect already executes SCHEMA_SQL


# ─────────────────────── Insert ───────────────────────


def insert_sandbox_simulation(
    *,
    event_ticker: str,
    event_date: str,
    event_type: str,
    event_text_hash: str,
    persona_set_hash: str,
    model_default: str,
    seed: int,
    n_personas: int,
    n_rounds: int,
    initial_price: float,
    final_price: float,
    cumulative_delta_pct: float,
    price_trajectory: list[float],
    class_pnl: dict[str, float],
    strategic_signals: dict[str, dict[str, Any]] | None,
    lambda_used: float,
    adv_used: float,
    full_report_path: str | None,
    cost_usd: float,
    elapsed_seconds: float,
    simulation_id: str | None = None,
    round_fingerprints: list[list[str | None]] | None = None,
    llm_seed: int | None = None,
) -> str:
    """Insert one sandbox-mode simulation row. Returns the simulation_id.

    Sentiment-mode columns (sentiment_mean, implied_move_*, blind_spots_json)
    are NULL for sandbox runs. Sandbox-specific columns (price_trajectory_json,
    class_pnl_json, lambda_used, adv_used, etc.) are populated.

    `mode` column is set to 'sandbox'.
    """
    sim_id = simulation_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    # round_fingerprints for sandbox is a list of lists (one inner list per
    # round, one fingerprint per persona class). Stringify the whole thing.
    fingerprints_json = (
        json.dumps(round_fingerprints) if round_fingerprints is not None else None
    )
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO simulations (
                simulation_id, event_ticker, event_date, event_type,
                event_text_hash, persona_set_hash, model_default,
                seed, n_personas, n_rounds,
                full_report_path, cost_usd, elapsed_seconds, created_at,
                round_fingerprints_json, llm_seed,
                mode, initial_price, final_price, cumulative_delta_pct,
                price_trajectory_json, class_pnl_json, strategic_signals_json,
                lambda_used, adv_used
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sim_id, event_ticker, event_date, event_type,
                event_text_hash, persona_set_hash, model_default,
                seed, n_personas, n_rounds,
                full_report_path, cost_usd, elapsed_seconds, now,
                fingerprints_json, llm_seed,
                'sandbox', initial_price, final_price, cumulative_delta_pct,
                json.dumps(price_trajectory),
                json.dumps(class_pnl, ensure_ascii=False),
                json.dumps(strategic_signals, ensure_ascii=False) if strategic_signals else None,
                lambda_used, adv_used,
            ),
        )
    return sim_id


# ─────────────────────── Update / Query ───────────────────────


def update_actual_move(
    simulation_id: str, *, first_day: float, first_week: float | None = None
) -> bool:
    """Manually backfill the actual market outcome for OOD scorecard tracking."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE simulations
            SET actual_first_day_move = ?,
                actual_first_week_move = ?,
                diff_recorded_at = ?
            WHERE simulation_id = ?
            """,
            (first_day, first_week, now, simulation_id),
        )
        return cur.rowcount > 0


def query_recent(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent N simulations as dicts."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM simulations ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in cur.fetchall()]


def get_simulation(simulation_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM simulations WHERE simulation_id = ?", (simulation_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None


__all__ = [
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "get_simulation",
    "init_db",
    "insert_sandbox_simulation",
    "query_recent",
    "update_actual_move",
]
