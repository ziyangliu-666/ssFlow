"""Flask API for ssFlow.

Endpoints
---------

Public:
    GET  /healthz                       — no auth, returns {"status": "ok"}
    GET  /                              — serves the SPA (frontend/dist or web/index.html)
    GET  /assets/<path>                 — Vite-built static assets

Authed (X-Auth-Password header):
    POST /extract-event                 — legacy: text-only event extraction (kept for tests)
    POST /simulate                      — legacy: synchronous run with personas YAML path
    GET  /report/<simulation_id>        — fetch a stored markdown report
    GET  /cost                          — current cost tracker state

    POST /session                       — create a new session, returns {session_id}
    POST /upload                        — multipart upload, returns {files: [{tmp_path, name, kind}]}
    POST /extract                       — files+urls+prompt → EventProposal + persona partials
    POST /personas/template             — blank persona template for "+ add" button
    POST /simulate-stream/init          — register a streaming-sim payload, returns {stream_id}
    GET  /simulate-stream/<stream_id>   — SSE stream of engine events (one-shot)

The streaming flow ties /upload → /extract → /simulate-stream together via a
single `session_id` issued at /session create time. The stream_id from
/simulate-stream/init is the auth token for the GET — it's a single-use
uuid4 hex with a 60s TTL, so EventSource (which can't send custom headers)
can still use it safely.

The legacy `/simulate` and `/extract-event` endpoints stay for backwards
compatibility with the existing CLI and test suite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from ssflow.config import settings
from ssflow.document_ingestion import build_corpus, ingest_many
from ssflow.event import VALID_EVENT_TYPES, Event
from ssflow.event_bus import ListSink, QueueSink, TeeSink
from ssflow.event_extractor import extract_event
from ssflow.llm_client import BudgetExceeded, cost_tracker
from ssflow.oasis_engine import run_simulation
from ssflow.persona import load_personas, persona_set_hash
from ssflow.persona_proposer import (
    blank_partial,
    persona_from_partial,
    propose_personas,
)
from ssflow.report import render_simulation_safe_or_quarantine, save_report
from ssflow.distillation import distill_sync
from ssflow.sandbox_generator import build_from_template
from ssflow.scorecard import get_simulation, init_db, insert_sandbox_simulation
from ssflow.session_store import get_default_store

from .auth import require_password


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ssflow.api")


# Where /upload writes user files. Cleaned up by lazy GC tied to the session.
UPLOAD_ROOT = settings.project_root / "tmp" / "uploads"


def _ensure_upload_root() -> Path:
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    return UPLOAD_ROOT


def _spa_root() -> Path | None:
    """Return the Vite build output directory, or None if not built.

    The legacy hand-written `web/` directory was removed in 2026-04-09.
    If `frontend/dist/index.html` doesn't exist, the user is expected
    to either run `npx vite` in `frontend/` (dev mode on :5173) or
    `npm run build` to produce a fresh `frontend/dist` (prod mode served
    directly by Flask on :5001).
    """
    dist = settings.project_root / "frontend" / "dist"
    if (dist / "index.html").exists():
        return dist
    return None


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    init_db()
    store = get_default_store()

    # ─────────────────────── Health / static / legacy ───────────────────────

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "version": "0.0.3"})

    @app.get("/")
    def index():
        root = _spa_root()
        if root is None:
            return jsonify({"error": "no_frontend_built"}), 404
        return send_from_directory(root, "index.html")

    @app.get("/assets/<path:filename>")
    def assets(filename: str):
        """Serve Vite-hashed assets from frontend/dist/assets."""
        root = _spa_root()
        if root is None:
            return jsonify({"error": "no_frontend_built"}), 404
        return send_from_directory(root / "assets", filename)

    @app.get("/favicon.ico")
    def favicon():
        root = _spa_root()
        if root is None or not (root / "favicon.ico").exists():
            return ("", 204)
        return send_from_directory(root, "favicon.ico")

    @app.get("/setup")
    @app.get("/setup/")
    @app.get("/run/<path:tail>")
    @app.get("/reports/<path:tail>")
    def spa_routes(tail: str | None = None):
        """Catchall for the Vue Router history-mode routes.

        These paths are owned by the SPA, not by the Flask API. We must
        serve index.html when a user reloads on /run/<id> or /reports/<id>
        so the SPA can take over routing client-side.
        """
        root = _spa_root()
        if root is None:
            return jsonify({"error": "no_frontend_built"}), 404
        return send_from_directory(root, "index.html")

    # ─────────────────────── Sessions ───────────────────────

    @app.post("/session")
    @require_password
    def create_session():
        state = store.create_session()
        return jsonify(
            {
                "session_id": state.session_id,
                "created_at": state.created_at,
            }
        )

    # ─────────────────────── /upload ───────────────────────

    @app.post("/upload")
    @require_password
    def upload():
        """Multipart upload of files. Returns the saved tmp paths.

        Form fields:
            files: one or more file parts (input name "files")
            session_id: optional — if missing, a new session is created
        """
        session_id = request.form.get("session_id") or request.args.get("session_id")
        if not session_id:
            session_id = store.create_session().session_id
        else:
            if store.get_session(session_id) is None:
                # Create a fresh state for unknown session_ids so the UI
                # can reuse a stale id without errors.
                store.create_session()  # eager GC
                session_id = store.create_session().session_id

        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "no_files"}), 400

        upload_root = _ensure_upload_root()
        session_dir = upload_root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        saved: list[dict[str, object]] = []
        for f in files:
            if not f or not f.filename:
                continue
            # Strip directory components to keep all uploads flat.
            safe_name = Path(f.filename).name
            target = session_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
            f.save(target)
            saved.append(
                {
                    "tmp_path": str(target),
                    "name": safe_name,
                    "size": target.stat().st_size,
                    "kind": target.suffix.lower().lstrip("."),
                }
            )

        # Append to whatever the session already tracked
        existing = []
        state = store.get_session(session_id)
        if state is not None:
            existing = list(state.uploaded_files)
        store.update_session(session_id, uploaded_files=existing + saved)

        return jsonify({"session_id": session_id, "files": saved})

    # ─────────────────────── /extract ───────────────────────

    @app.post("/extract")
    @require_password
    def extract():
        """Stage 0 — produce an EventProposal + a list of editable persona partials.

        Request JSON:
            {
                "session_id": "<uuid>",
                "prompt":     "free-form description of what to predict",
                "urls":       ["https://example.com/article", ...]
            }

        The endpoint:
          1. Loads files saved against the session via /upload
          2. Ingests them + any URLs into a corpus
          3. Runs `extract_event(prompt, pre_fetched_corpus=corpus)`
          4. Loads the suggested persona pack and projects to partials
          5. Returns {event_proposal, personas_proposed, session_id}
        """
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"error": "invalid_json"}), 400

        session_id = payload.get("session_id")
        if not session_id:
            session_id = store.create_session().session_id
        state = store.get_session(session_id)
        if state is None:
            state = store.create_session()
            session_id = state.session_id

        prompt = (payload.get("prompt") or "").strip()
        urls = payload.get("urls") or []
        if not isinstance(urls, list):
            urls = []

        if not prompt and not state.uploaded_files and not urls:
            return jsonify(
                {
                    "error": "empty_input",
                    "detail": "extract requires at least a prompt, a file, or a URL",
                }
            ), 400

        file_paths = [f["tmp_path"] for f in state.uploaded_files]

        try:
            ingested = asyncio.run(ingest_many(files=file_paths, urls=urls))
        except Exception as exc:
            log.exception("ingest_many failed")
            return jsonify({"error": "ingest_failed", "detail": str(exc)}), 500

        corpus = build_corpus(ingested)

        # The raw_input passed to the extractor is just the user's prompt.
        # Files and URLs become the corpus (above); the prompt carries the
        # user's intent. When the user only uploaded files with no prompt,
        # derive a stub from the first source title.
        if prompt:
            raw_input = prompt
        elif corpus:
            raw_input = (
                f"Analyze the uploaded documents about {corpus[0].get('title', 'an event')}"
            )
        else:
            raw_input = ""

        try:
            proposal = asyncio.run(
                extract_event(raw_input, pre_fetched_corpus=corpus or None)
            )
        except BudgetExceeded as exc:
            return jsonify({"error": "budget_exceeded", "detail": str(exc)}), 429
        except Exception as exc:
            log.exception("extract_event failed")
            return jsonify({"error": "extraction_failed", "detail": str(exc)}), 500

        proposal_dict = proposal.to_dict()

        # Propose personas from the suggested pack
        base_pack_path = (
            proposal.suggested_personas_path or "personas/ashare.yaml"
        )
        try:
            personas_proposed = propose_personas(
                event=proposal.to_event(),
                base_pack_path=base_pack_path,
            )
        except Exception as exc:
            log.warning(
                "propose_personas failed for %s: %s — falling back to ashare",
                base_pack_path,
                exc,
            )
            try:
                personas_proposed = propose_personas(
                    base_pack_path="personas/ashare.yaml"
                )
                base_pack_path = "personas/ashare.yaml"
            except Exception as exc2:
                log.exception("fallback persona proposal failed")
                return jsonify(
                    {"error": "persona_proposal_failed", "detail": str(exc2)}
                ), 500

        # Persist on the session for convenience.
        store.update_session(
            session_id,
            event_proposal=proposal_dict,
            personas_proposed=personas_proposed,
            prompt=prompt,
            urls=urls,
        )

        return jsonify(
            {
                "session_id": session_id,
                "event_proposal": proposal_dict,
                "personas_proposed": personas_proposed,
                "base_personas_path": base_pack_path,
                "ingested_docs": [
                    {
                        "source": d.source,
                        "kind": d.kind,
                        "char_count": d.char_count,
                        "title": d.title,
                        "warnings": d.warnings,
                    }
                    for d in ingested
                ],
            }
        )

    # ─────────────────────── /personas/template ───────────────────────

    @app.post("/personas/template")
    @require_password
    def personas_template():
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            payload = {}
        decision_mode = (payload.get("decision_mode") or "discretionary").strip()
        return jsonify(blank_partial(decision_mode))

    # ─────────────────────── /simulate-stream/init ───────────────────────

    @app.post("/simulate-stream/init")
    @require_password
    def simulate_stream_init():
        """Validate the payload, resolve the personas + event, and stash
        them under a one-shot stream_id for the EventSource GET to claim."""
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"error": "invalid_json"}), 400

        event_data = payload.get("event") or {}
        personas_partials = payload.get("personas") or []
        n_rounds = payload.get("n_rounds") or settings.n_rounds
        seed = payload.get("seed")
        base_personas_path = (
            payload.get("base_personas_path") or "personas/ashare.yaml"
        )
        session_id = payload.get("session_id")

        if not isinstance(personas_partials, list) or not personas_partials:
            return jsonify(
                {"error": "personas_required", "detail": "personas list must be non-empty"}
            ), 400

        # Validate the event
        try:
            event = _build_event_from_dict(event_data)
        except (TypeError, ValueError) as exc:
            return jsonify({"error": "invalid_event", "detail": str(exc)}), 400

        if not event.is_sandbox_ready:
            return jsonify(
                {
                    "error": "sandbox_not_ready",
                    "detail": "event requires current_price and adv_value > 0",
                }
            ), 400

        # Load the base pack to resolve partials → full Personas
        try:
            base_pack = load_personas(Path(base_personas_path))
        except Exception as exc:
            return jsonify(
                {"error": "base_pack_load_failed", "detail": str(exc)}
            ), 400

        try:
            personas = [
                persona_from_partial(p, base_pack=base_pack)
                for p in personas_partials
            ]
        except Exception as exc:
            log.exception("persona_from_partial failed")
            return jsonify(
                {"error": "persona_resolve_failed", "detail": str(exc)}
            ), 400

        if not personas:
            return jsonify({"error": "no_resolved_personas"}), 400

        # Entity graph + instrument universe + round schedule (optional)
        entity_graph_data = payload.get("entity_graph")
        instrument_universe_data = payload.get("instrument_universe")
        round_schedule_data = payload.get("round_schedule")

        # Stash everything in the store. The actual run happens when the
        # client opens the SSE GET.
        stream_id = store.register_stream_payload(
            payload={
                "event": _event_to_dict(event),
                "personas": [_persona_to_storable(p) for p in personas],
                "personas_partials": personas_partials,
                "n_rounds": int(n_rounds),
                "seed": seed,
                "base_personas_path": base_personas_path,
                "entity_graph": entity_graph_data,
                "instrument_universe": instrument_universe_data,
                "round_schedule": round_schedule_data,
            },
            session_id=session_id,
        )

        return jsonify({"stream_id": stream_id, "n_personas": len(personas)})

    # ─────────────────────── /simulate-stream/<id> ───────────────────────

    @app.get("/simulate-stream/<stream_id>")
    def simulate_stream(stream_id: str):
        """Open the SSE channel and run the simulation in a background thread.

        NOTE: this route does NOT use @require_password — the stream_id
        IS the auth token. It was issued by /simulate-stream/init (which
        is auth'd) and is single-use + TTL'd to 60 seconds. EventSource
        cannot send custom headers, so this is the only way to make
        SSE work without query-param auth (which has its own pitfalls
        around access logs).
        """
        entry = store.claim_stream_payload(stream_id)
        if entry is None:
            return jsonify({"error": "stream_not_found_or_already_claimed"}), 404

        sim_payload = entry.payload
        try:
            event = _build_event_from_dict(sim_payload["event"])
        except (TypeError, ValueError) as exc:
            return jsonify({"error": "invalid_event_in_payload", "detail": str(exc)}), 500

        # Re-resolve personas from the stored partials so we get fresh
        # Persona objects (the dataclass is not trivially picklable through
        # the in-memory dict, so we re-hydrate via the same path).
        base_personas_path = sim_payload.get("base_personas_path", "personas/ashare.yaml")
        try:
            base_pack = load_personas(Path(base_personas_path))
            personas = [
                persona_from_partial(p, base_pack=base_pack)
                for p in sim_payload["personas_partials"]
            ]
        except Exception as exc:
            log.exception("re-hydrate personas failed")
            return jsonify({"error": "rehydrate_failed", "detail": str(exc)}), 500

        n_rounds = int(sim_payload.get("n_rounds") or settings.n_rounds)
        seed = sim_payload.get("seed")

        # Rebuild EntityGraph from stashed data (if present)
        entity_graph = None
        entity_graph_data = sim_payload.get("entity_graph")
        if entity_graph_data and settings.enable_entity_sandbox:
            try:
                entity_graph = _rebuild_entity_graph(entity_graph_data, event)
            except Exception as exc:
                log.warning("Failed to rebuild entity graph, running without: %s", exc)
                entity_graph = None

        # Rebuild InstrumentUniverse from stashed data (if present)
        instrument_universe = None
        iu_data = sim_payload.get("instrument_universe")
        if iu_data:
            try:
                from ssflow.instrument import InstrumentUniverse
                instrument_universe = InstrumentUniverse.from_serializable(iu_data)
            except Exception as exc:
                log.warning("Failed to rebuild instrument universe: %s", exc)

        # Rebuild RoundSchedule from stashed data (if present)
        round_schedule = None
        rs_data = sim_payload.get("round_schedule")
        if rs_data:
            try:
                from ssflow.round_schedule import RoundSchedule
                round_schedule = RoundSchedule.from_serializable(rs_data)
            except Exception as exc:
                log.warning("Failed to rebuild round schedule: %s", exc)

        q: "queue.Queue[dict | None]" = queue.Queue(maxsize=2000)
        # Capture the full event log in-memory so we can persist it to
        # reports/<sim_id>.events.json when the simulation finishes —
        # this is what powers /simulation/:id/timeline and the replay view.
        event_log = ListSink()

        def engine_thread() -> None:
            try:
                result = asyncio.run(
                    run_simulation(
                        event,
                        personas,
                        n_rounds=n_rounds,
                        seed=seed,
                        event_sink=TeeSink(QueueSink(q), event_log),
                        entity_graph=entity_graph,
                        instrument_universe=instrument_universe,
                        round_schedule=round_schedule,
                    )
                )
                # Render + persist the report
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
                # Persist the full in-memory event log so /simulation/:id/timeline
                # can replay it later. Written atomically via a sibling temp file
                # to avoid a half-written JSON on crash.
                try:
                    events_path = settings.reports_dir / f"{sim_id}.events.json"
                    tmp_path = events_path.with_suffix(".events.json.tmp")
                    tmp_path.write_text(
                        json.dumps(
                            {
                                "simulation_id": sim_id,
                                "n_rounds": result.n_rounds,
                                "n_personas": result.n_personas,
                                "events": event_log.events,
                            },
                            ensure_ascii=False,
                            default=_json_default,
                        ),
                        encoding="utf-8",
                    )
                    tmp_path.replace(events_path)
                except Exception:
                    log.exception("failed to persist event log for %s", sim_id)
                q.put(
                    {
                        "type": "simulation_done",
                        "simulation_id": sim_id,
                        "report_markdown": display,
                        "report_path": str(report_path),
                        "compliance_passed": ok,
                        "publication_log": publication_log,
                        "class_pnl": class_pnl,
                        "price_trajectory": result.price_trajectory,
                        "initial_price": result.initial_price,
                        "final_price": result.final_price,
                        "cumulative_delta_pct": result.cumulative_delta_pct,
                        "elapsed_seconds": result.elapsed_seconds,
                        "cost_usd": result.cost_usd,
                    }
                )
            except BudgetExceeded as exc:
                log.warning("stream sim hit budget: %s", exc)
                q.put(
                    {"type": "error", "code": "budget_exceeded", "detail": str(exc)}
                )
            except Exception as exc:
                log.exception("streaming sim failed")
                q.put(
                    {"type": "error", "code": "simulation_failed", "detail": str(exc)}
                )
            finally:
                q.put(None)

        thread = threading.Thread(target=engine_thread, daemon=True)
        thread.start()

        def sse_generator():
            yield "retry: 5000\n\n"
            yield ': open\n\n'
            last_heartbeat = time.time()
            while True:
                try:
                    event_msg = q.get(timeout=1.0)
                except queue.Empty:
                    if time.time() - last_heartbeat > 15:
                        yield ": keepalive\n\n"
                        last_heartbeat = time.time()
                    continue
                if event_msg is None:
                    return
                event_type = str(event_msg.get("type", "message"))
                yield (
                    f"event: {event_type}\n"
                    f"data: {json.dumps(event_msg, ensure_ascii=False, default=_json_default)}\n\n"
                )
                last_heartbeat = time.time()

        return Response(
            stream_with_context(sse_generator()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ─────────────────────── Legacy endpoints (kept) ───────────────────────

    @app.post("/extract-event")
    @require_password
    def extract_event_endpoint():
        """Legacy: text-only Stage 0 extraction (no file upload)."""
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"error": "invalid_json"}), 400

        raw_input = (payload.get("input") or "").strip()
        if not raw_input:
            return jsonify({"error": "input_required"}), 400

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
        """Legacy: synchronous run with personas YAML path. Used by CLI + tests."""
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"error": "invalid_json"}), 400

        try:
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
            return jsonify(
                {
                    "error": "sandbox_not_ready",
                    "detail": "ssFlow requires current_price and adv_value in the request body",
                }
            ), 400

        personas_path = payload.get("personas_path")
        if not personas_path:
            return jsonify(
                {
                    "error": "personas_path_required",
                    "detail": "personas_path must be specified — ssFlow has no default market pack",
                }
            ), 400

        try:
            personas = load_personas(Path(personas_path))
        except Exception as exc:
            return jsonify({"error": "personas_load_failed", "detail": str(exc)}), 500

        try:
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

        return jsonify(
            {
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
            }
        )

    @app.get("/report/<simulation_id>")
    @require_password
    def get_report(simulation_id: str):
        """Return a simulation's report as structured JSON.

        Response shape:
            {
              "simulation_id": str,
              "markdown": str,                # full rendered report
              "summary": {                    # top-line numbers for the 4-cell strip
                "initial_price": float,
                "final_price": float,
                "delta_pct": float,          # cumulative delta as a fraction
                "net_flow_total": float,      # sum of per-class P&L
                "winning_class": {            # class with the highest P&L
                  "archetype": str,
                  "pnl": float,
                } | null,
                "price_trajectory": [float],
                "per_class_pnl": [            # sorted desc by P&L
                  {"persona_id": str, "archetype": str, "pnl": float}
                ],
              },
              "meta": {
                "event_ticker": str,
                "event_date": str,
                "event_type": str,
                "n_personas": int,
                "n_rounds": int,
                "lambda_used": float,
                "adv_used": float,
                "elapsed_seconds": float,
                "cost_usd": float,
                "created_at": str,
                "price_currency": str,       # inferred from the scorecard row
              },
            }

        Raw markdown can still be fetched by passing `?format=md` if a
        client wants the compact form for copy-paste or download.
        """
        fmt = (request.args.get("format") or "").strip().lower()

        md_path = settings.reports_dir / f"{simulation_id}.md"
        if not md_path.exists():
            return jsonify({"error": "not_found", "simulation_id": simulation_id}), 404

        markdown = md_path.read_text(encoding="utf-8")

        if fmt == "md":
            return (
                markdown,
                200,
                {"Content-Type": "text/markdown; charset=utf-8"},
            )

        # JSON response — merge in the scorecard row for the structured fields
        row = get_simulation(simulation_id)
        if row is None:
            # Report file exists but scorecard row is missing (e.g. legacy
            # file or manual placement). Return the markdown + empty summary
            # rather than a 404.
            return jsonify({
                "simulation_id": simulation_id,
                "markdown": markdown,
                "summary": None,
                "meta": None,
                "warning": "scorecard row not found for this simulation_id",
            })

        # Decode the JSON blobs stored in the scorecard
        try:
            price_trajectory = json.loads(row.get("price_trajectory_json") or "[]")
        except Exception:
            price_trajectory = []
        try:
            class_pnl = json.loads(row.get("class_pnl_json") or "{}")
        except Exception:
            class_pnl = {}

        # Resolve persona_id → archetype labels. The scorecard only stores
        # persona ids, but the UI wants the archetype (e.g. "散户动量派")
        # for display. Try to rehydrate via the most recently matched
        # personas pack stored in publications, falling back to the id itself.
        archetype_by_id: dict[str, str] = {}
        try:
            pub_log = json.loads(row.get("publication_log_json") or "[]")
            for pub in pub_log:
                pid = pub.get("author_persona_id")
                atype = pub.get("author_archetype")
                if pid and atype:
                    archetype_by_id.setdefault(pid, atype)
        except Exception:
            pass

        per_class_pnl = sorted(
            [
                {
                    "persona_id": pid,
                    "archetype": archetype_by_id.get(pid, pid),
                    "pnl": float(pnl),
                }
                for pid, pnl in class_pnl.items()
            ],
            key=lambda entry: entry["pnl"],
            reverse=True,
        )

        net_flow_total = sum(entry["pnl"] for entry in per_class_pnl)
        winning = per_class_pnl[0] if per_class_pnl else None

        # Currency: look up from the scorecard row if present; otherwise
        # fall back to CNY (A-share default).
        price_currency = "CNY"

        summary = {
            "initial_price": row.get("initial_price"),
            "final_price": row.get("final_price"),
            "delta_pct": row.get("cumulative_delta_pct"),
            "net_flow_total": net_flow_total,
            "winning_class": (
                {"archetype": winning["archetype"], "pnl": winning["pnl"]}
                if winning is not None else None
            ),
            "price_trajectory": price_trajectory,
            "per_class_pnl": per_class_pnl,
        }
        meta = {
            "event_ticker": row.get("event_ticker"),
            "event_date": row.get("event_date"),
            "event_type": row.get("event_type"),
            "n_personas": row.get("n_personas"),
            "n_rounds": row.get("n_rounds"),
            "lambda_used": row.get("lambda_used"),
            "adv_used": row.get("adv_used"),
            "elapsed_seconds": row.get("elapsed_seconds"),
            "cost_usd": row.get("cost_usd"),
            "created_at": row.get("created_at"),
            "price_currency": price_currency,
        }

        return jsonify({
            "simulation_id": simulation_id,
            "markdown": markdown,
            "summary": summary,
            "meta": meta,
        })

    @app.get("/simulation/<simulation_id>/timeline")
    @require_password
    def get_simulation_timeline(simulation_id: str):
        """Return the full event log for a completed simulation.

        Powers the `/replay/:id` view and the Report round inspector.

        Response shape:
            {
              "simulation_id": str,
              "n_rounds": int,
              "n_personas": int,
              "events": [{"type": str, "ts": float, ...type-specific...}],
              "source": "full" | "reconstructed",
            }

        - `source="full"`: read from `reports/<sim_id>.events.json`,
          which is written atomically when a sim completes. Contains
          every event the SSE stream emitted (thoughts, trades, flows,
          prices, round boundaries).
        - `source="reconstructed"`: fallback for simulations that
          finished before event-log persistence existed. Rebuilds a
          partial timeline from `publication_log_json` and
          `price_trajectory_json` — missing thoughts and trades, but
          still enough to render a round-by-round inspector and a
          price chart.
        """
        events_path = settings.reports_dir / f"{simulation_id}.events.json"
        if events_path.exists():
            try:
                data = json.loads(events_path.read_text(encoding="utf-8"))
                data.setdefault("simulation_id", simulation_id)
                data["source"] = "full"
                return jsonify(data)
            except Exception as exc:
                log.exception("failed to read events.json for %s", simulation_id)
                return jsonify({
                    "error": "events_file_unreadable",
                    "detail": str(exc),
                }), 500

        # Fallback: reconstruct a partial timeline from scorecard blobs.
        row = get_simulation(simulation_id)
        if row is None:
            return jsonify({
                "error": "not_found",
                "simulation_id": simulation_id,
            }), 404

        try:
            price_trajectory = json.loads(row.get("price_trajectory_json") or "[]")
        except Exception:
            price_trajectory = []
        try:
            publications = json.loads(row.get("publication_log_json") or "[]")
        except Exception:
            publications = []

        n_rounds = int(row.get("n_rounds") or 0)
        n_personas = int(row.get("n_personas") or 0)
        initial_price = row.get("initial_price") or (
            price_trajectory[0] if price_trajectory else 0.0
        )
        final_price = row.get("final_price") or (
            price_trajectory[-1] if price_trajectory else 0.0
        )

        # Derive a pseudo-timestamp by round so the frontend can display
        # something sensible in the time column. Real wall-clock is lost
        # for reconstructed timelines.
        base_ts = 0.0

        events: list[dict] = []
        events.append({
            "type": "simulation_start",
            "ts": base_ts,
            "n_personas": n_personas,
            "n_rounds": n_rounds,
            "initial_price": initial_price,
        })

        # Bucket publications by round.
        pubs_by_round: dict[int, list[dict]] = {}
        for pub in publications:
            r = int(pub.get("round_idx") or 0)
            pubs_by_round.setdefault(r, []).append(pub)

        for r in range(max(n_rounds, len(price_trajectory) - 1)):
            round_ts = base_ts + (r + 1)
            price_before = (
                price_trajectory[r] if r < len(price_trajectory) else initial_price
            )
            price_after = (
                price_trajectory[r + 1]
                if r + 1 < len(price_trajectory)
                else price_before
            )
            events.append({
                "type": "round_start",
                "ts": round_ts,
                "round_idx": r,
                "current_price": price_before,
            })
            for pub in pubs_by_round.get(r, []):
                # Render publications as persona_thought events so the
                # existing TimelineEvent component can display them with
                # no new rendering code. author_archetype is the cleanest
                # display label we have.
                events.append({
                    "type": "persona_thought",
                    "ts": round_ts + 0.1,
                    "round_idx": r,
                    "persona_id": pub.get("author_persona_id") or "",
                    "archetype": pub.get("author_archetype")
                    or pub.get("author_persona_id")
                    or "",
                    "content_type": pub.get("content_type") or "social_post",
                    "text": pub.get("text") or "",
                    "likes": pub.get("likes") or 0,
                    "reposts": pub.get("reposts") or 0,
                })
            delta_pct = (
                (price_after / price_before - 1.0) if price_before else 0.0
            )
            events.append({
                "type": "price_updated",
                "ts": round_ts + 0.5,
                "round_idx": r,
                "price_before": price_before,
                "price_after": price_after,
                "delta_pct": delta_pct,
                "net_flow_total": 0.0,
            })
            events.append({
                "type": "round_complete",
                "ts": round_ts + 0.6,
                "round_idx": r,
                "publications_count": len(pubs_by_round.get(r, [])),
                "orders_count": 0,
            })

        events.append({
            "type": "simulation_complete",
            "ts": base_ts + n_rounds + 1,
            "initial_price": initial_price,
            "final_price": final_price,
            "cumulative_delta_pct": row.get("cumulative_delta_pct") or 0.0,
            "elapsed_seconds": row.get("elapsed_seconds") or 0.0,
            "price_trajectory": price_trajectory,
        })

        return jsonify({
            "simulation_id": simulation_id,
            "n_rounds": n_rounds,
            "n_personas": n_personas,
            "events": events,
            "source": "reconstructed",
        })

    @app.get("/cost")
    @require_password
    def get_cost():
        return jsonify(
            {
                "total_cost_usd": cost_tracker.total_cost_usd,
                "total_calls": cost_tracker.total_calls,
                "by_model": cost_tracker.cost_by_model,
                "budget_usd": settings.budget_usd,
            }
        )

    # ─────────────────────── /distill ───────────────────────

    @app.post("/distill")
    @require_password
    def distill_endpoint():
        """Distill an event topic into an InstrumentUniverse.

        Identifies the primary instrument + related instruments, fetches
        real market data (prices, K-lines) for all of them, and returns
        a serialized InstrumentUniverse + a default RoundSchedule.

        Request body:
            topic: str (required)
            market: str (optional, default "ashare")
            event_ticker: str (optional) — if already known
            event_price: float (optional) — if already fetched
            event_date: str (optional) — for round schedule
        """
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"error": "invalid_json"}), 400

        topic = payload.get("topic", "").strip()
        if not topic:
            return jsonify({"error": "topic_required"}), 400

        market = payload.get("market", "ashare")
        event_ticker = payload.get("event_ticker")
        event_price = payload.get("event_price")
        event_date = payload.get("event_date", "")

        try:
            universe = distill_sync(
                topic=topic,
                market=market,
                event_ticker=event_ticker,
                event_price=float(event_price) if event_price else None,
            )
        except Exception as exc:
            log.exception("distillation failed")
            return jsonify({"error": "distillation_failed", "detail": str(exc)}), 500

        # Generate a round schedule — use preset from request or default
        from ssflow.round_schedule import make_schedule
        schedule_preset = payload.get("schedule_preset", "earnings-5d")
        schedule_spec = payload.get("schedule_spec")  # custom spec overrides preset
        schedule = make_schedule(
            preset=schedule_preset,
            event_date=event_date or "TBD",
            spec=schedule_spec,
        )

        return jsonify({
            "instrument_universe": universe.to_serializable(),
            "round_schedule": schedule.to_serializable(),
        })

    # ─────────────────────── /sandbox/generate ───────────────────────

    @app.post("/sandbox/generate")
    @require_password
    def sandbox_generate():
        """Generate an EntityGraph from a topic string.

        Uses templates + optional LLM tuning. Returns a serialized
        EntityGraph that the frontend can display and pass back to
        /simulate-stream/init.

        Request body:
            topic: str (required) — e.g. "比亚迪 2026Q1 业绩不及预期"
            market: str (optional, default "ashare")
            current_price: float (optional)
            use_llm: bool (optional, default false) — whether to call LLM for tuning
            entity_overrides: dict (optional) — per-slot overrides
        """
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"error": "invalid_json"}), 400

        topic = payload.get("topic", "").strip()
        if not topic:
            return jsonify({"error": "topic_required", "detail": "topic string is required"}), 400

        market = payload.get("market", "ashare")
        current_price = payload.get("current_price", 0.0)
        use_llm = payload.get("use_llm", False)
        entity_overrides = payload.get("entity_overrides")

        try:
            if use_llm:
                from ssflow.sandbox_generator import generate_sandbox
                graph = generate_sandbox(
                    topic=topic,
                    market=market,
                    current_price=float(current_price or 0),
                )
            else:
                graph = build_from_template(
                    topic=topic,
                    market=market,
                    current_price=float(current_price or 0),
                    entity_overrides=entity_overrides,
                )
            return jsonify(graph.to_serializable())
        except Exception as exc:
            log.exception("sandbox generation failed")
            return jsonify({"error": "sandbox_generation_failed", "detail": str(exc)}), 500

    return app


# ─────────────────────── Helpers ───────────────────────


def _rebuild_entity_graph(data: dict, event: "Event") -> "EntityGraph":
    """Rebuild an EntityGraph from the serialized JSON.

    Now that to_serializable() includes condition_expr and full effect
    details, we can faithfully reconstruct the graph — including
    LLM-tuned thresholds — without falling back to build_from_template.
    """
    from ssflow.entity import (
        Entity, EntityGraph, EntityState, ResourceFlow,
        Threshold, ThresholdEffect, compile_condition,
    )

    graph = EntityGraph(
        topic=data.get("topic", ""),
        generated_at=data.get("generated_at", ""),
        template_used=data.get("template_used", ""),
    )

    # Rebuild entities
    for eid, e_data in data.get("entities", {}).items():
        graph.add_entity(Entity(
            id=eid,
            display_name=e_data.get("display_name", eid),
            entity_type=e_data.get("entity_type", "custom"),
            state=EntityState(variables={
                k: float(v) for k, v in e_data.get("state", {}).items()
            }),
            state_labels=dict(e_data.get("state_labels", {})),
            persona_id=e_data.get("persona_id"),
        ))

    # Rebuild flows (source_var/target_var preserved; no gate needed)
    for f_data in data.get("flows", []):
        try:
            graph.add_flow(ResourceFlow(
                id=f_data["id"],
                source_id=f_data["source_id"],
                target_id=f_data["target_id"],
                resource_type=f_data.get("resource_type", "resource"),
                rate_per_round=float(f_data.get("rate_per_round", 1.0)),
                source_var=f_data.get("source_var", ""),
                target_var=f_data.get("target_var", ""),
                label=f_data.get("label", ""),
            ))
        except (KeyError, ValueError) as exc:
            log.warning("Skipping flow rebuild: %s", exc)

    # Rebuild thresholds — compile condition from condition_expr
    for t_data in data.get("thresholds", []):
        cond_expr = t_data.get("condition_expr", "")
        if not cond_expr:
            log.warning(
                "Threshold '%s' has no condition_expr, skipping",
                t_data.get("id", "?"),
            )
            continue
        try:
            condition = compile_condition(cond_expr)
            effect_type = t_data.get("effect_type", "inject_event")
            if effect_type == "force_action":
                effect = ThresholdEffect(
                    effect_type="force_action",
                    forced_side=t_data.get("forced_side", ""),
                    forced_quantity_pct=float(t_data.get("forced_quantity_pct", 0.0)),
                )
            elif effect_type == "mutate_state":
                effect = ThresholdEffect(
                    effect_type="mutate_state",
                    state_mutations={
                        k: float(v)
                        for k, v in t_data.get("state_mutations", {}).items()
                    },
                )
            else:
                effect = ThresholdEffect(
                    effect_type="inject_event",
                    event_text=t_data.get("event_text", ""),
                    event_author_id=t_data.get("event_author_id", ""),
                    event_content_type=t_data.get("event_content_type", "company_announcement"),
                )
            graph.register_threshold(Threshold(
                id=t_data.get("id", f"rebuilt_{cond_expr}"),
                entity_id=t_data["entity_id"],
                description=t_data.get("description", cond_expr),
                condition=condition,
                effect=effect,
                condition_expr=cond_expr,
                cooldown_rounds=int(t_data.get("cooldown_rounds", 2)),
            ))
        except (KeyError, ValueError) as exc:
            log.warning("Skipping threshold rebuild '%s': %s", t_data.get("id", "?"), exc)

    return graph


def _build_event_from_dict(data: dict) -> Event:
    """Construct an Event from the loose JSON shape we accept on the wire."""
    adv = data.get("adv_value")
    if adv is None:
        adv = data.get("adv_cny")
    event_type = data.get("event_type", "other")
    if event_type not in VALID_EVENT_TYPES:
        event_type = "other"
    return Event(
        ticker=str(data.get("ticker", "")).strip(),
        event_text=str(data.get("event_text", "")).strip(),
        event_type=event_type,
        event_date=str(data.get("event_date", "")).strip(),
        prior_consensus=str(data.get("prior_consensus", "")).strip(),
        recent_price_action=str(data.get("recent_price_action", "")).strip(),
        sector_context=str(data.get("sector_context", "")).strip(),
        current_price=data.get("current_price"),
        adv_value=adv,
        market=data.get("market"),
        price_currency=data.get("price_currency", "CNY"),
        instrument=data.get("instrument"),
    )


def _event_to_dict(event: Event) -> dict:
    return {
        "ticker": event.ticker,
        "event_text": event.event_text,
        "event_type": event.event_type,
        "event_date": event.event_date,
        "prior_consensus": event.prior_consensus,
        "recent_price_action": event.recent_price_action,
        "sector_context": event.sector_context,
        "current_price": event.current_price,
        "adv_value": event.adv_value,
        "market": event.market,
        "price_currency": event.price_currency,
        "instrument": event.instrument,
    }


def _persona_to_storable(persona) -> dict:
    """Lightweight projection of a Persona for the stream payload echo.

    The actual run uses the partials list (which we re-hydrate). This
    projection is just for diagnostics.
    """
    return {
        "id": persona.id,
        "archetype": persona.archetype,
        "entity_role": persona.entity_role,
    }


def _json_default(value):
    """Fallback JSON encoder for engine event payloads (handles dataclasses)."""
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(value)
    if isinstance(value, (set, frozenset)):
        return list(value)
    return str(value)


app = create_app()
