"""Flask API for ssFish — synchronous, single-user.

Endpoints:
    GET  /healthz                  — no auth, returns {"status": "ok"}
    POST /simulate                 — auth required, runs a simulation (blocks ~30s)
    GET  /report/<simulation_id>   — auth required, fetches stored markdown report
    GET  /                         — serves the static index.html form

The /simulate endpoint is intentionally blocking. At 10×5 scale latency is
~30 seconds, well within Flask's default request timeout. Async job queue is
deferred to Week 3+ when multi-user load shows up.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from ssfish.aggregation import aggregate
from ssfish.config import settings
from ssfish.event import VALID_EVENT_TYPES, Event
from ssfish.llm_client import BudgetExceeded, cost_tracker
from ssfish.persona import load_personas, persona_set_hash
from ssfish.report import (
    render_safe_or_quarantine,
    render_sandbox_safe_or_quarantine,
    save_report,
)
from ssfish.sandbox import run_sandbox_simulation
from ssfish.scorecard import init_db, insert_sandbox_simulation, insert_simulation
from ssfish.simulation import run_simulation

from .auth import require_password


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ssfish.api")


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    init_db()

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "version": "0.0.1"})

    @app.get("/")
    def index():
        web_dir = settings.project_root / "web"
        return send_from_directory(web_dir, "index.html")

    @app.post("/simulate")
    @require_password
    def simulate():
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"error": "invalid_json"}), 400

        mode = payload.get("mode", "sentiment")
        if mode not in {"sentiment", "sandbox"}:
            return jsonify({"error": "invalid_mode", "detail": f"mode must be 'sentiment' or 'sandbox', got {mode!r}"}), 400

        try:
            event = Event(
                ticker=payload.get("ticker", "").strip(),
                event_text=payload.get("event_text", "").strip(),
                event_type=payload.get("event_type", "other"),
                event_date=payload.get("event_date", "").strip(),
                prior_consensus=payload.get("prior_consensus", "").strip(),
                recent_price_action=payload.get("recent_price_action", "").strip(),
                sector_context=payload.get("sector_context", "").strip(),
                current_price=payload.get("current_price"),
                adv_cny=payload.get("adv_cny"),
            )
        except (TypeError, ValueError) as exc:
            return jsonify({"error": "invalid_event", "detail": str(exc)}), 400

        if mode == "sandbox" and not event.is_sandbox_ready:
            return jsonify({
                "error": "sandbox_not_ready",
                "detail": "sandbox mode requires current_price and adv_cny in the request body"
            }), 400

        personas_path = Path(payload.get("personas_path") or
                             settings.personas_dir / "ashare.yaml")
        try:
            personas = load_personas(personas_path)
        except Exception as exc:
            return jsonify({"error": "personas_load_failed", "detail": str(exc)}), 500

        if mode == "sandbox":
            return _run_sandbox_endpoint(event, personas)
        return _run_sentiment_endpoint(event, personas)

    def _run_sentiment_endpoint(event: Event, personas):
        try:
            result = asyncio.run(run_simulation(event, personas))
        except BudgetExceeded as exc:
            return jsonify({"error": "budget_exceeded", "detail": str(exc)}), 429
        except Exception as exc:
            log.exception("sentiment simulation failed")
            return jsonify({"error": "simulation_failed", "detail": str(exc)}), 500

        report = aggregate(result)
        display, ok = render_safe_or_quarantine(result, report)
        report_path = save_report(display, result.simulation_id)

        sim_id = insert_simulation(
            event_ticker=event.ticker,
            event_date=event.event_date,
            event_type=event.event_type,
            event_text_hash=event.text_hash,
            persona_set_hash=persona_set_hash(personas),
            model_default=settings.default_model,
            seed=settings.seed,
            n_personas=result.n_personas,
            n_rounds=result.n_rounds,
            sentiment_mean=report.sentiment_mean,
            sentiment_std=report.sentiment_std,
            implied_move_low=report.implied_move_low,
            implied_move_high=report.implied_move_high,
            implied_move_confidence=report.implied_move_confidence,
            blind_spots=report.blind_spots,
            full_report_path=str(report_path),
            cost_usd=result.cost_usd,
            elapsed_seconds=result.elapsed_seconds,
            simulation_id=result.simulation_id,
            round_fingerprints=result.round_fingerprints,
            llm_seed=result.llm_seed,
        )

        return jsonify({
            "mode": "sentiment",
            "simulation_id": sim_id,
            "report_markdown": display,
            "compliance_passed": ok,
            "implied_move_low": report.implied_move_low,
            "implied_move_high": report.implied_move_high,
            "implied_move_confidence": report.implied_move_confidence,
            "sentiment_mean": report.sentiment_mean,
            "sentiment_std": report.sentiment_std,
            "elapsed_seconds": result.elapsed_seconds,
            "cost_usd": result.cost_usd,
            "report_path": str(report_path),
        })

    def _run_sandbox_endpoint(event: Event, personas):
        try:
            result = asyncio.run(run_sandbox_simulation(event, personas))
        except BudgetExceeded as exc:
            return jsonify({"error": "budget_exceeded", "detail": str(exc)}), 429
        except Exception as exc:
            log.exception("sandbox simulation failed")
            return jsonify({"error": "simulation_failed", "detail": str(exc)}), 500

        display, ok = render_sandbox_safe_or_quarantine(result)
        report_path = save_report(display, result.simulation_id)

        # Strategic signals from final round
        strategic_signals = {}
        by_id = {p.id: p for p in personas}
        if result.rounds:
            last = result.rounds[-1]
            for cid, cf in last.class_flows.items():
                p = by_id.get(cid)
                if p and p.contributes_to_strategic_signal and cf.strategic_signal:
                    strategic_signals[cid] = cf.strategic_signal

        fingerprints_per_round = [r.fingerprints for r in result.rounds]
        class_pnl = result.compute_class_pnl()

        sim_id = insert_sandbox_simulation(
            event_ticker=event.ticker,
            event_date=event.event_date,
            event_type=event.event_type,
            event_text_hash=event.text_hash,
            persona_set_hash=persona_set_hash(personas),
            model_default=settings.default_model,
            seed=settings.seed,
            n_personas=result.n_personas,
            n_rounds=result.n_rounds,
            initial_price=result.initial_price,
            final_price=result.final_price,
            cumulative_delta_pct=result.cumulative_delta_pct,
            price_trajectory=result.price_trajectory,
            class_pnl=class_pnl,
            strategic_signals=strategic_signals or None,
            lambda_used=result.lambda_used,
            adv_used=result.adv_cny_used,
            full_report_path=str(report_path),
            cost_usd=result.cost_usd,
            elapsed_seconds=result.elapsed_seconds,
            simulation_id=result.simulation_id,
            round_fingerprints=fingerprints_per_round,
            llm_seed=result.llm_seed,
        )

        return jsonify({
            "mode": "sandbox",
            "simulation_id": sim_id,
            "report_markdown": display,
            "compliance_passed": ok,
            "initial_price": result.initial_price,
            "final_price": result.final_price,
            "cumulative_delta_pct": result.cumulative_delta_pct,
            "price_trajectory": result.price_trajectory,
            "class_pnl": class_pnl,
            "strategic_signals": strategic_signals or None,
            "lambda_used": result.lambda_used,
            "adv_used": result.adv_cny_used,
            "elapsed_seconds": result.elapsed_seconds,
            "cost_usd": result.cost_usd,
            "report_path": str(report_path),
        })

    @app.get("/report/<simulation_id>")
    @require_password
    def get_report(simulation_id: str):
        path = settings.reports_dir / f"{simulation_id}.md"
        if not path.exists():
            return jsonify({"error": "not_found"}), 404
        return path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/markdown; charset=utf-8"}

    @app.get("/cost")
    @require_password
    def get_cost():
        return jsonify({
            "total_cost_usd": cost_tracker.total_cost_usd,
            "total_calls": cost_tracker.total_calls,
            "by_model": cost_tracker.cost_by_model,
            "budget_usd": settings.budget_usd,
        })

    return app


app = create_app()
