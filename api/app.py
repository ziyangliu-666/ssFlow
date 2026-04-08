"""Flask API for ssFish — sandbox-only.

Endpoints:
    GET  /healthz                  — no auth, returns {"status": "ok"}
    POST /simulate                 — auth required, runs a sandbox simulation (~18-30s)
    GET  /report/<simulation_id>   — auth required, fetches stored markdown report
    GET  /cost                     — auth required, current cost tracker state
    GET  /                         — serves the static index.html form

The /simulate endpoint is intentionally synchronous. At ~14 personas × 5 rounds
the latency is ~18-30 seconds, well within Flask's default request timeout.
Async job queue is deferred to a future scaling phase.

The legacy `sentiment` mode endpoint was removed in the G2 cleanup —
sandbox is the only execution mode.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from ssfish.config import settings
from ssfish.event import VALID_EVENT_TYPES, Event
from ssfish.event_extractor import extract_event
from ssfish.llm_client import BudgetExceeded, cost_tracker
from ssfish.oasis_engine import run_simulation
from ssfish.persona import load_personas, persona_set_hash
from ssfish.report import render_simulation_safe_or_quarantine, save_report
from ssfish.scorecard import init_db, insert_sandbox_simulation

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

    @app.post("/extract-event")
    @require_password
    def extract_event_endpoint():
        """Stage 0 — auto-extract an EventProposal from a free-form input string.

        Request:  {"input": "NVIDIA Q1 earnings beat..."}
        Response: EventProposal as JSON (all fields auto-filled, with confidence)

        The user reviews the response in the UI, edits any field, and submits
        the (possibly edited) Event back to /simulate. This stage is the
        autonomous "deep research" step that turns 1 input into 10 fields.

        Cost: ~$0.02-0.04. Wall clock: ~20-40 seconds.
        """
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"error": "invalid_json"}), 400

        raw_input = (payload.get("input") or "").strip()
        if not raw_input:
            return jsonify({"error": "input_required", "detail": "field 'input' must be a non-empty string"}), 400

        try:
            proposal = asyncio.run(extract_event(raw_input))
        except BudgetExceeded as exc:
            return jsonify({"error": "budget_exceeded", "detail": str(exc)}), 429
        except Exception as exc:
            log.exception("event extraction failed")
            return jsonify({"error": "extraction_failed", "detail": str(exc)}), 500

        return jsonify(proposal.to_dict())

    @app.post("/simulate")
    @require_password
    def simulate():
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"error": "invalid_json"}), 400

        try:
            # Accept both adv_value (new) and adv_cny (legacy alias) field names
            adv = payload.get("adv_value")
            if adv is None:
                adv = payload.get("adv_cny")
            event = Event(
                ticker=payload.get("ticker", "").strip(),
                event_text=payload.get("event_text", "").strip(),
                event_type=payload.get("event_type", "other"),
                event_date=payload.get("event_date", "").strip(),
                prior_consensus=payload.get("prior_consensus", "").strip(),
                recent_price_action=payload.get("recent_price_action", "").strip(),
                sector_context=payload.get("sector_context", "").strip(),
                current_price=payload.get("current_price"),
                adv_value=adv,
                market=payload.get("market"),
                price_currency=payload.get("price_currency", "CNY"),
                instrument=payload.get("instrument"),
            )
        except (TypeError, ValueError) as exc:
            return jsonify({"error": "invalid_event", "detail": str(exc)}), 400

        if not event.is_sandbox_ready:
            return jsonify({
                "error": "sandbox_not_ready",
                "detail": "ssFish requires current_price and adv_value in the request body",
            }), 400

        personas_path = payload.get("personas_path")
        if not personas_path:
            return jsonify({
                "error": "personas_path_required",
                "detail": "personas_path must be specified — ssFish has no default market pack",
            }), 400

        try:
            personas = load_personas(Path(personas_path))
        except Exception as exc:
            return jsonify({"error": "personas_load_failed", "detail": str(exc)}), 500

        try:
            # OASIS engine is async; Flask is sync, so wrap in asyncio.run.
            result = asyncio.run(run_simulation(event, personas))
        except BudgetExceeded as exc:
            return jsonify({"error": "budget_exceeded", "detail": str(exc)}), 429
        except Exception as exc:
            log.exception("OASIS simulation failed")
            return jsonify({"error": "simulation_failed", "detail": str(exc)}), 500

        display, ok = render_simulation_safe_or_quarantine(result)
        report_path = save_report(display, result.simulation_id)

        publication_log = [
            {
                "publication_id": pub.publication_id,
                "author_persona_id": pub.author_persona_id,
                "author_archetype": pub.author_archetype,
                "content_type": pub.content_type,
                "text": pub.text,
                "round_idx": pub.round_idx,
                "authority_weight": pub.authority_weight,
                "references": pub.references,
                "oasis_post_id": pub.oasis_post_id,
                "likes": pub.likes,
                "reposts": pub.reposts,
            }
            for pub in result.all_publications
        ]
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
            strategic_signals=None,
            lambda_used=result.lambda_used,
            adv_used=result.adv_value_used,
            full_report_path=str(report_path),
            cost_usd=result.cost_usd,
            elapsed_seconds=result.elapsed_seconds,
            simulation_id=result.simulation_id,
            round_fingerprints=None,
            llm_seed=result.llm_seed,
            publication_log=publication_log or None,
            oasis_db_path=result.oasis_db_path,
        )

        return jsonify({
            "simulation_id": sim_id,
            "report_markdown": display,
            "compliance_passed": ok,
            "initial_price": result.initial_price,
            "final_price": result.final_price,
            "cumulative_delta_pct": result.cumulative_delta_pct,
            "price_trajectory": result.price_trajectory,
            "class_pnl": class_pnl,
            "publication_log": publication_log,
            "lambda_used": result.lambda_used,
            "adv_used": result.adv_value_used,
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
