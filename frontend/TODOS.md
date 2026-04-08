# Frontend follow-ups

Status as of **2026-04-09** — after three consecutive autonomous QA passes
(Pass 1 happy path, Pass 2 edges, Pass 3 responsive 1440/1100/860/500,
Pass 4 keyboard + a11y) against the real Flask + Vite + yourapi.cn stack.

## ✅ Shipped

### Design review cycle (2026-04-08 → 2026-04-09)

- **Editorial redesign** — Home, Setup, Run, Report all use the same
  typography (Inter + Noto Serif SC + Fraunces italic + JetBrains Mono),
  shared `<WorkflowRail>`, one orange accent per page.
- **Gap 1** — `/report/<id>` returns `{markdown, summary, meta}` JSON.
- **Gap 2** — `oasis_engine` emits `persona_thought` before
  `trade_submitted` within each round (causal order).
- **Gap 3** — Run page per-class flow shows `archetype`, not
  `persona_id`.
- **Gap 4** — dead `extra_text` field removed from both ends.
- **#10 ⌘K** — global shortcut focuses the composer from anywhere.
- **#18 truncate 14** — winning-class archetype on the report summary
  shows 14 chars instead of 10, and gets a hover title attribute for
  the full string.

### Autonomous QA cycle (2026-04-09, after sleep request)

- **#4 file restoration** — When the user returns to Home from Setup
  (back nav, router push, or page reload), previously uploaded files
  are shown as read-only `restored` chips with a `saved` italic badge.
  Clicking the × drops the file from the session store too.
- **#5 composer error banner** — Any failure during
  /session → /upload → /extract now surfaces a bold red alert banner
  with: title (`Authentication failed` / `Rate limited` / `Server
  error` / `Network error`), detail, and — for 401 specifically — a
  hint pointing the user at the sidebar `Settings` button. Status
  text is cleared on error so there's no ambiguity.
- **#6 responsive <860px** — `<aside class="rail">` collapses from a
  220px left sidebar into a 47px sticky top strip with
  `flex-direction: row`. Workflow step labels hide on narrow; only the
  dots + the active step's label + cell show. Sidebar footer
  (settings + auth input) moves to the right side of the strip.
- **#6 Home layout <860** — Main padding shrinks, `h1` drops to 30px
  (24px below 560), composer-foot wraps so the `⌘⏎` hint drops to
  its own line.
- **#6 Setup layout <860** — Bottom bar anchors to `left: 0` instead
  of `left: var(--ss-sidebar-w)`. Event-form and persona-grid stack.
  `base-pack` metadata hides to save space.
- **#6 Run layout <1100** — Grid collapses to single-column; live
  state panel caps at 40vh, timeline scrolls below it. At <860 the
  whole view becomes vertical scroll (no overflow hidden) with
  shrunken typography.
- **#6 Report summary strip** — 4-col at ≥860, 2-col (2×2) at 860-500,
  1-col stacked at <500. Never overflows horizontally.
- **#7 a11y landmarks** — `<nav aria-label="Workflow steps">` wraps
  the workflow rail. `<main aria-label="Seed the simulation">` on
  Home. Brand link has `aria-label="ssFlow home"`. Rail `<ol>` items
  carry `aria-current="step"` on the active one.
- **#7 focus indicators** — Every keyboard-tabbable element on Home
  has a visible `:focus-visible` ring:
  - `submit` button: orange box-shadow halo (outline: none)
  - `ta` textarea: inset orange box-shadow (composer border is
    secondary indicator via `:focus-within`)
  - `brand` link: orange box-shadow + underline
  - `paperclip`, `gear`, `example`: default browser outline
  Verified via a headless Chromium Tab-navigation test — **every
  tabbable element has a visible focus indicator**.
- **#7 whole-card click** — `PersonaCard` article now has
  `role="button"`, `tabindex="0"`, `aria-expanded`, Enter/Space
  keyboard toggle, and an `onCardClick` handler that bails out only
  when the click lands on an input/textarea/button/body element.
  Previously only the `header.p-h` region was clickable.
- **#15 Report 401 UX** — Report error state now shows a specific
  "Auth not set" hint with a `Settings` pointer when the error is a
  401. Added a `Retry` button alongside the existing `← Back to Seed`.
- **#16 Vite `/report/` proxy regression guard** — The autonomous
  Playwright tests exercise `/reports/:id` as a deep link, which
  catches the proxy-prefix bug if it ever regresses.
- **Non-TODO bug found & fixed**: `api/auth.py` imported
  `settings` by name (`from ssflow.config import settings`) which
  bound the reference at import time. Tests that swap
  `ssflow.config.settings` via re-init still hit the old object, so
  the fixture's new password was invisible to the auth decorator.
  Fixed by importing the module (`from ssflow import config as
  _config`) and dereferencing on every request. Surfaced because the
  mixed e2e + non-e2e run broke on the /report endpoint tests.

## Open — lower priority

### 8. Color contrast audit
- **What:** Run the new palette through a WCAG AA checker. Specifically
  verify: `--ss-fg-faint` (#a3a3a3) on white for non-essential text,
  `--ss-accent` (#ff6a00) on white for headlines, `--ss-fg-muted`
  (#6b6b6b) on `--ss-bg-soft` (#f7f7f7) for sidebar sub-labels.
- **Why:** The new palette is lighter than the old one. Some of these
  may fail AA. Can be done with a headless axe-core scan.

### 9. Extract the composer into a reusable component
- **What:** `Home.vue` has a ~250-line `<div class="composer">` block
  that's likely to be reused if we ever add a "follow-up question"
  flow after Simulate. Extract to `src/components/Composer.vue` with
  props for `placeholder`, `examples`, `onSubmit`.
- **Why:** Not urgent — no second use site exists yet.

### 17. Hashtag / emoji rendering in persona thoughts
- **What:** LLM sometimes produces `#股市 #短线追涨 #BYD` inside
  persona_thought text. Currently rendered as plain text. Could
  detect `#tag` and render as dim mono chips.
- **Why:** Cosmetic. Low value.

### 19. Engine sometimes produces zero trades in a round
- **What:** In small-N sims (2 agents × 2 rounds), the LLM may post a
  social tweet without calling `submit_order_distribution`. The
  engine treats that as "hold all" but the UI shows
  "(no orders yet)" which misleads the user.
- **Fix options:** (a) hard-retry the trader on the second
  `max_iteration`, (b) show "held by default" tooltip on empty panel,
  (c) downshift the panel's visual emphasis when the round ended with
  zero explicit flows.
- **File:** `src/ssflow/oasis_engine.py` + `src/views/SimulationRunView.vue`
