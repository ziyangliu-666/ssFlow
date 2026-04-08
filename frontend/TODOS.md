# Frontend follow-ups from 2026-04-08 design review

Captured during `/plan-design-review`. Home.vue has been rewritten to match
the approved mockup. These items are everything else that the review
surfaced but was deliberately deferred to keep this PR focused.

## High — unlocks the rest of the redesign

### 1. Rewrite `SetupView.vue` against `setup-redesign.html`
- **What:** Apply the same `<WorkflowRail>` + Noto Serif SC + Fraunces italic
  language to the Confirm step. Merge the two-column persona split
  (Traders | Info entities) into a single grid with filter pills
  (`All N / Traders N / Info entities N`). Remove the duplicate count from
  the section header. Rebuild `EventProposalForm` so labels use the
  borderless underline style from the mockup.
- **Why:** Step 02 currently still shows the old terminal aesthetic with
  the `STEP 02 · CONFIRM` topbar tag — a jarring break from Home now that
  Home is redesigned.
- **Reference:** `~/.gstack/projects/ziyangliu-666-ssFlow/designs/home-unified-composer-20260408/setup-redesign.html`

### 2. Rewrite `SimulationRunView.vue` against `run-redesign.html`
- **What:** Collapse the four stacked black-bordered cards (`PRICE
  TRAJECTORY / ROUNDS / PER-CLASS FLOW / META`) into a single live-state
  panel where the rounds strip is the chart's x-axis. Rebuild
  `TimelineEvent` with the new marker-style event card (serif italic type
  label + circular marker, orange fill for trade, black for price, outline
  for round). Move `RUNNING` status chip to the sidebar footer; remove the
  `STEP 03` topbar tag. Add the Fraunces italic orange emphasis to key
  words inside thought logs (`割肉止损`, `加仓`, `做空`).
- **Why:** The live run view is the most data-dense page in the product —
  the existing 4-card layout wastes the left column on chrome instead of
  context.
- **Reference:** `run-redesign.html`

### 3. Rewrite `ReportView.vue` against `report-redesign.html`
- **What:** Replace the `v-html="marked(markdown)"` dump with a structured
  layout: kicker + italic-accent headline + 4-cell summary strip + markdown
  body styled with the new serif/accent rules. `New sim` / `Download .md`
  move to the sidebar footer.
- **Why:** The report is the handoff artifact — it should read like a
  research note, not a raw markdown dump.
- **Reference:** `report-redesign.html`

## Medium — visible gaps in Home.vue

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
- **Why:** Design review Pass 6 flagged responsive as the weakest dimension
  (3/10 → 5/10 after fixes, still the lowest).
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
- **File:** All views.

### 8. Color contrast audit
- **What:** Run the new palette through a WCAG AA checker. Specifically
  verify: `--ss-fg-faint` (#a3a3a3) on white for non-essential text,
  `--ss-accent` (#ff6a00) on white for headlines, `--ss-fg-muted`
  (#6b6b6b) on `--ss-bg-soft` (#f7f7f7) for sidebar sub-labels.
- **Why:** The new palette is lighter than the old one (`#e6e6e6` →
  `#ececec`, `#999999` → `#a3a3a3`). Some of these may fail AA.

## Low — nice to have

### 9. Extract the composer into a reusable component
- **What:** `Home.vue` has a 200-line `<div class="composer">` block that's
  likely to be reused if we ever add a "follow-up question" flow after
  Simulate. Extract to `src/components/Composer.vue` with props for
  placeholder/examples/onSubmit.
- **Why:** Not urgent — no second use site exists yet. But the code smell
  is visible.

### 10. `example.usage` keyboard binding
- **What:** Add ⌘K / Ctrl+K to focus the composer from anywhere on the page.
  Shift+Enter inside the textarea should insert a newline (default); ⌘⏎
  already submits.
- **Why:** Small DX win for heavy users.

### 11. Structured report data for the summary strip
- **What:** Extend the backend `/report/<id>` endpoint (or add
  `/report/<id>/summary`) to return structured fields: `final_price`,
  `delta_pct`, `net_flow_total`, `winning_class_archetype`,
  `winning_class_pnl`. Use them to render the 4-cell summary strip on
  Report (see `report-redesign.html` — the orange headline number + the
  strip under the deck were the most visually striking part of the mockup
  and are currently missing from the implementation).
- **Why:** The current Report view applies the new typography to the
  markdown body but lacks the editorial-style summary strip because there
  is no structured data channel. The UI code in `ReportView.vue` is
  designed around a markdown blob; adding the strip requires either a
  backend change or a fragile markdown-regex parse.
- **Reference:** `report-redesign.html` — the `.summary` grid section.

### 12. `runExtract` no longer uses `extraText`
- **What:** Home composer now folds everything into the single `prompt`
  field and passes `extraText: ''` to `runExtract`. The backend
  `/extract` endpoint still accepts an `extra_text` body field. Either
  remove the field from the backend schema or document that it is
  deprecated and always empty from the new frontend.
- **Why:** Dead API surface — harmless, but will confuse future backend
  maintainers.
- **File:** `api/app.py` (look for the extract endpoint schema).
