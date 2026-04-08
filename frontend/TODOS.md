# Frontend follow-ups from 2026-04-08 design review

Status as of **2026-04-08 23:55** — after the full backend audit, Gap 1-4
fixes, E2E pipeline test, and live Playwright walk-through on the real
Flask + Vite stack.

## ✅ Done in this PR (was deferred, now shipped)

### 1. ~~Rewrite `SetupView.vue`~~ — shipped in `feat(frontend)` commit
### 2. ~~Rewrite `SimulationRunView.vue`~~ — shipped in `feat(frontend)` commit
### 3. ~~Rewrite `ReportView.vue`~~ — shipped in `feat(frontend)` commit
### 11. ~~Structured report data for the summary strip~~ — Gap 1 fix
- `GET /report/<id>` now returns `{markdown, summary, meta}` JSON.
- `summary` carries `initial_price`, `final_price`, `delta_pct`,
  `net_flow_total`, `winning_class`, `price_trajectory`, `per_class_pnl`.
- `?format=md` fallback returns raw markdown for legacy clients.
- Verified against real sim data via `test_e2e_smoke.py` and live
  Playwright walk-through: summary cells render ¥210.76 / -3.54% /
  −¥2798.40万 / retail_pa… with correct color coding.

### 12. ~~`runExtract` no longer uses `extraText`~~ — Gap 4 fix
- Removed from `Home.vue`, `api/extract.js`, `store/session.js`.
- Removed from backend `/extract` request schema in `api/app.py`.

### 13. ~~Run page showed persona_id instead of archetype~~ — Gap 3 fix
- `SimulationRunView.vue` now reads `payload.archetype` off
  `class_flow_computed` events and keys the per-class flow panel by
  `persona_id` while displaying the archetype label. Verified via
  `test_full_pipeline_byd_earnings` (real sim).

### 14. ~~oasis_engine emitted thoughts AFTER trades~~ — Gap 2 fix
- `run_simulation` now queries publications + emits `persona_thought`
  events right after `env.step` and BEFORE draining the OrderCollector.
  Market-broadcaster posts are filtered out of persona_thought (they're
  already represented by `price_updated`).
- Verified by the e2e test assertion: within each round, all
  `persona_thought` indices must be strictly less than any
  `trade_submitted` index.

## High — still open

### 4. File restoration when returning from `/setup`
- **What:** When the user navigates back from `/setup` to `/`, the
  `files` ref is empty (only `session.uploadedFiles` has the server-side
  refs). Show already-uploaded files as read-only chips on the Home
  composer with a small "remove from session" action.
- **Why:** Right now hitting back from Setup and then tweaking the prompt
  silently loses the visual of which files were attached.
- **File:** `src/views/Home.vue:150`

### 5. Composer error states
- **What:** Specifically design + build: (a) upload failure (one file
  succeeds, one fails — which chip gets the error?), (b) URL fetch failure
  inside `/extract`, (c) auth failure distinct from 500.
- **Why:** Design review Pass 2 flagged this as partial coverage. Happy
  path ships; the edges need explicit UI.
- **File:** `src/views/Home.vue:onStart`

### 6. Responsive below 860px
- **What:** At widths < 860px, the sidebar should collapse into a horizontal
  top strip (brand + current step + tap-to-expand for history). Right now
  the sidebar just takes 220px out of the viewport, which kills mobile.
- **Why:** Design review Pass 6 flagged responsive as the weakest dimension.
- **File:** `src/components/WorkflowRail.vue`

## Medium — accessibility

### 7. Landmark roles + keyboard nav audit
- **What:** Add `<main>` / `<aside>` / `<nav>` landmarks where missing.
  Verify every interactive element is keyboard-reachable. Add visible
  focus rings (the composer has one; the submit button, paperclip, and
  sidebar gear don't). Audit touch target sizes against 44×44 minimum.
- **Why:** a11y was rated 3/10 in review; we said 5/10 after this PR only
  because the composer focus state was added. Everything else is still
  owed.

### 8. Color contrast audit
- **What:** Run the new palette through a WCAG AA checker. Specifically
  verify: `--ss-fg-faint` (#a3a3a3) on white for non-essential text,
  `--ss-accent` (#ff6a00) on white for headlines, `--ss-fg-muted`
  (#6b6b6b) on `--ss-bg-soft` (#f7f7f7) for sidebar sub-labels.

## New — surfaced by the real E2E walk-through

### 15. `ReportView` loses the session when navigating back to `/reports/<id>`
- **What:** Right now `/reports/<id>` is a stateful route — it needs the
  auth password in `session.password` to fetch the report. A direct
  navigation from outside the SPA (e.g. a bookmark or a refresh) works
  only because the password is in localStorage. If localStorage was
  cleared, the GET fails with 401 and shows the error state.
- **Why:** Minor UX paper cut. The error page says "Can't load this
  report" but doesn't tell the user to set the auth password.
- **Fix:** On the error branch, if the response status was 401, prompt
  the user to re-enter the password inline (same UI as the sidebar
  settings toggle) instead of just a "Back to Seed" button.

### 16. Vite proxy `/report` vs `/reports` bug already fixed — but add a test
- **What:** I already fixed `vite.config.js` to scope the proxy to
  `/report/` (trailing slash) so it doesn't eat the frontend router
  path `/reports/<id>`. Add a tiny smoke test that GETs
  `http://127.0.0.1:5173/reports/abc` during dev and asserts it returns
  the SPA index.html, not a 500 from the proxy. Prevents regression.

### 17. `content_type` emoji / markdown in thoughts leaks into the timeline
- **What:** In the real sim I just ran, one persona_thought text was
  `"BYD earnings超预期! 🚀感觉市场回暖了，可以关注一下。🤔今年要翻身了！ #股市 #短线追涨 #BYD"`
  (LLM emitted emoji + hashtags). The timeline renders this raw via
  `v-html="highlightThought(payload.text)"` with HTML-escaping. It looks
  fine but the hashtags aren't clickable chips. Low-priority cosmetic.
- **Fix:** Optional — detect `#tag` inside persona_thought text and
  render as a dim mono chip with a line-height boundary.

### 18. Per-class flow panel shows `retail_pa…` truncated to 10 chars
- **What:** `truncate(summary.winning_class.archetype, 10)` in
  `ReportView.vue` is too aggressive for short English archetype
  prefixes like `retail_passive`. Bump to 14.
- **File:** `src/views/ReportView.vue` (the `truncate` call on the
  winning cell).

### 19. Event ordering assertion is stronger than what the engine currently produces
- **What:** In one of the real runs, Round 0 had 1 persona_thought but
  0 trade_submitted (the LLM only posted a tweet, didn't call the
  trading tool). The Gap 2 assertion is guarded against this (it only
  fires when both exist) but the engine producing zero trades in a
  round is a UX issue — the per-class flow panel shows "(no orders
  yet)" which might mislead the user.
- **Fix:** Either force every trader to call `submit_order_distribution`
  at least once per round (OASIS agent config change), or add a tooltip
  explaining "held by default" on the empty per-class panel.

## Low — nice to have

### 9. Extract the composer into a reusable component
- **What:** `Home.vue` has a 200-line `<div class="composer">` block that's
  likely to be reused if we ever add a "follow-up question" flow after
  Simulate. Extract to `src/components/Composer.vue` with props for
  placeholder/examples/onSubmit.

### 10. `⌘K` keyboard binding to focus composer
- **What:** Add ⌘K / Ctrl+K to focus the composer from anywhere on the page.
