# ssFlow frontend — design system

Approved in design review on 2026-04-08. This document is the source of truth
for the frontend visual language. If something in the code disagrees with this
file, fix the code.

## The redesign in one sentence

**One conversational composer in the middle of an editorial page, framed by
a persistent workflow rail on the left, with a single orange italic accent
that reappears as the signature gesture across every view.**

## What this replaced (and why)

The pre-redesign frontend had:

1. **Five separate input fields on Home** (auth password, file upload, prompt,
   extra context, URLs). Collapsed to **one composer**: textarea + drag-drop +
   paperclip + URL auto-detect. Password moved into the sidebar footer
   (persisted in `localStorage`).
2. **Duplicate `01/02/03/04` numbering** — workflow steps on the left panel
   and input field numbers on the right panel. Killed; step numbers now
   appear only in the shared `<WorkflowRail>` sidebar.
3. **Six layers of brand/status text** (`ssFlow v0.1`, `Market Event
   Simulation Engine`, `Phase II Preview`, `沙盒推演…`, `SYSTEM READY`, the
   workflow label). Reduced to one `ssFlow` brand mark (with italic accent)
   in the sidebar, one headline on each page.
4. **`STEP 02 · CONFIRM` style tags** in every view's topbar. Removed; the
   shared `<WorkflowRail>` already communicates step status.
5. **Persona count written 3 times** on Setup. Down to one (on the active
   filter pill).
6. **Four stacked card borders** on Run (`PRICE TRAJECTORY / ROUNDS /
   PER-CLASS FLOW / META`). Collapsed into one live-state panel where rounds
   act as the chart's x-axis.
7. **Terminal-cosplay typography** (uppercase tracked labels on every
   container). Uppercase is now reserved for small metadata labels only
   (`CURRENT`, `Δ %`, `ADV`, etc.).

## Approved mockups

Always cross-reference these when implementing. They are the visual contract.

| Screen | Mockup | HTML source |
|---|---|---|
| Home (variant B2 hybrid) | `~/.gstack/projects/ziyangliu-666-ssFlow/designs/home-unified-composer-20260408/variant-B2.png` | `variant-B2-serif-hybrid.html` |
| Setup | `~/.gstack/projects/ziyangliu-666-ssFlow/designs/home-unified-composer-20260408/setup-redesign.png` | `setup-redesign.html` |
| Run | `~/.gstack/projects/ziyangliu-666-ssFlow/designs/home-unified-composer-20260408/run-redesign.png` | `run-redesign.html` |
| Report | `~/.gstack/projects/ziyangliu-666-ssFlow/designs/home-unified-composer-20260408/report-redesign.png` | `report-redesign.html` |

All four HTML files are standalone — open any of them in a browser to see
the intended layout at 1:1 fidelity.

## Voice — zh-primary, native, not translated

The product ships **Chinese-primary for all UI chrome.** Audience is
A-share, CNY, Chinese personas, Chinese examples, Chinese extracted
reports. Headlines, labels, buttons, empty states, error banners,
filter chips, footer disclaimers — all Chinese.

**Native, not translated.** Write each language from scratch using
native vocabulary and idiom. Do NOT direct-translate English
metaphors into Chinese. The zh and en headlines on the same page can
say completely different things as long as both express the intent.

**Leave in Latin script:** tickers (`BYD`, `300750`), prices (`¥390.80`),
percentages (`+56.69%`), dates (`2026-04-09`), simulation IDs, small
domain-symbol labels (`ADV`, `λ`), and the `ssFlow` brand mark.

## Typography roles

Four type families, each with a single job. Do not cross the streams.

| Role | Family | When to use |
|---|---|---|
| Body / UI / labels | **Inter** (400/500/600) | Buttons, paragraphs, form labels, menu text |
| CJK headlines | **Noto Serif SC** (500) | Every Chinese `h1` and `h2`, persona names |
| Accent / italic | **Fraunces** italic | Inline emphasis words, brand wordmark (`ss*Flow*`), section titles (`Per-class *P&L*`), small italic section headers (`Try an example`, `Event proposal`) |
| Numbers / data | **JetBrains Mono** (400/500) | Prices, Δ%, tickers, stream ids, timestamps, any tabular number |

**Never** use JetBrains Mono for body text. **Never** use uppercase with
letter-spacing 0.08em as a decorative pattern — that's the terminal cosplay
we just removed.

Fonts are loaded in `frontend/index.html` via Google Fonts. CSS helper
classes are defined in `src/assets/global.css`: `.ss-serif-cjk`,
`.ss-serif-accent`, `.ss-mono`.

## Color — one accent, used sparingly

| Token | Value | Usage |
|---|---|---|
| `--ss-accent` | `#ff6a00` | Brand italic, one inline headline word per page, active workflow step, focus states, hover on primary buttons |
| `--ss-accent-soft` | `#fff4ec` | Drag-over background, gentle highlights |
| `--ss-fg` | `#0a0a0a` | Body text, primary buttons (solid fill) |
| `--ss-fg-muted` | `#6b6b6b` | Secondary text, labels |
| `--ss-fg-faint` | `#a3a3a3` | Tertiary metadata, placeholders, dashed separators |
| `--ss-line` | `#ececec` | Subtle dividers |
| `--ss-line-strong` | `#cfcfcf` | Composer borders, prominent dividers |
| `--ss-good` | `#1d7a3a` | Positive P&L, BUY side |
| `--ss-bad` | `#c43c3c` | Negative P&L, SELL side |

**Rule:** orange appears *at most once* as a loud decoration per screen. The
brand mark in the sidebar + the one headline accent word is fine (they're in
different visual zones). A page with an orange button + orange active step +
orange brand + orange text accent is over-saturated — pick one loud use.

## The signature gesture — colored accent word inside a headline

This is ssFlow's visual fingerprint. Every page has one Noto Serif SC headline
with exactly one orange accent word embedded in it. The product ships
Chinese-primary (see "Voice" below), so the accent is a **Noto Serif SC
weight-600 character in `--ss-accent` orange** — no italic, because
synthesized italic on CJK looks bad. Fraunces italic is reserved for
Latin-only accents (the `ssFlow` brand mark, small eyebrow text, and
number callouts like `*-3.5%*`).

| Page | Headline |
|---|---|
| Home | 一条消息，如何在市场里 *发酵*。 |
| Setup | 确认要 *推演* 的事件。 |
| Run | 看它 *发酵*。 |
| Replay | 重播 *消化*。 |
| Report | 业绩端利空，被市场折价 *-3.5%* 吸收。 |

**Rotating verb pool.** On Home, Run, and Replay, the accent word cycles
every 2.8s through a native A-share editorial pool:
`发酵 → 消化 → 兑现 → 搅动 → 推演`. All five are 2-character verbs so
the layout is stable by construction. The cycle is disabled entirely
under `prefers-reduced-motion`. Implementation: `<transition
mode="out-in">` with `.rip-enter-*` + `.rip-leave-*` fade/slide.

When you write a new page, follow the pattern. Pick the one word or number
that carries the page's meaning and set it in the accent class (Noto
Serif SC 600 + `--ss-accent`, or Fraunces italic + `--ss-accent` if the
accent is a Latin number or the brand).

### English reference pool (not shipped)

If an English surface is ever added (i18n toggle, global landing page,
developer docs), use this pool as the canonical equivalent. It is NOT
a translation of the zh pool — both are written natively in their
language. EN uses Fraunces italic as the accent typography.

| Page | Headline |
|---|---|
| Home | Watch a headline *ripple* into a price. |
| Setup | Confirm what to *simulate*. |
| Run | Watching it *ripple*. |
| Replay | Replay the *ripple*. |
| Report | A sharp miss on margins, absorbed into a *-3.5%* day. |

**Rotating verb pool (EN):** `ripple → fold → bleed → echo → cascade`.

## Layout — persistent left sidebar

Every view is a CSS grid with `grid-template-columns: var(--ss-sidebar-w) 1fr`
where `--ss-sidebar-w: 220px`. The left column is `<WorkflowRail>` (shared
component, `src/components/WorkflowRail.vue`).

The rail shows:
- `ssFlow` brand (Fraunces wordmark) at top
- 4-step workflow (`01 Seed · 02 Confirm · 03 Simulate · 04 Report`)
- Current step is orange italic with a filled orange dot; done steps are
  solid black; future steps are outline gray
- Sidebar footer holds the settings/auth toggle and a one-line disclaimer

The main column belongs to the page. Pages must not add their own topbar
with a `STEP 02` tag — the rail is the progress indicator.

## The composer (Home only — but its language extends everywhere)

One `<div class="composer">` contains:
- A chips row that shows attached files and auto-detected URLs
- A borderless textarea (no visible border, placeholder is a 3-line example)
- A footer row with: paperclip tool, hint text, status text, primary submit

Composer states:
- `focused` → border becomes `--ss-fg`, shadow intensifies
- `drag-over` → border becomes `--ss-accent`, background fills with
  `--ss-accent-soft`

Submit button is solid black with a right arrow, turns orange on hover.
**Never** put the submit button outside the composer.

URLs inside the prompt are auto-detected by the regex
`/https?:\/\/[^\s<>"')\]]+/g`, shown as chips, and stripped from the text
before being sent to `/extract` (they go in the `urls` field instead).

## Spacing scale

- 4px grid, but think in 14/18/22/32 for major gaps
- Vertical rhythm: hero → 34px → composer → 28px → examples → 42px → disclaimer
- Composer internal padding: 14/16 (top/sides) for text area, 10/12 for foot
- Sidebar internal padding: 22/20

## Borders — subtraction default

- `1px solid var(--ss-line)` for subtle separation (inside cards)
- `1px solid var(--ss-line-strong)` for the composer and the important dividers
- Dashed separators for "continuation" relationships (table row bottom, inside
  persona cards)
- **No** boxed card grids. A list of items gets `border-bottom: 1px dashed`,
  not 8 individual bordered rectangles.

## Responsive

- Breakpoint at 860px: main padding shrinks, h1 drops to 32px
- Below 860px, the sidebar should collapse to a top strip (not yet
  implemented — tracked in TODOS)
- Composer always fills its container width

## Accessibility notes (partial — more in TODOS)

- Submit button has explicit `:disabled` state with `opacity: 0.45`
- Hint text references `⌘⏎` keyboard shortcut (also handled in `onKeydown`)
- Focus states exist on composer and auth input
- **Not yet specified**: ARIA landmarks on `<main>`, `<aside>`, `<nav>`.
  Touch target sizes not audited. Color contrast not audited. See TODOS.

## When you add a new page

Checklist:
- [ ] Wrap it in a flex container alongside `<WorkflowRail />`
- [ ] Pick ONE headline with ONE orange accent word (Noto Serif SC 600
      for a CJK verb, Fraunces italic for a Latin number / the brand)
- [ ] Use Noto Serif SC for CJK headings, Inter for body, JetBrains Mono for
      numbers — no exceptions
- [ ] No topbar with a `STEP` tag — the rail already does that
- [ ] Audit for the old terminal vocabulary: `.panel-title`, `.card-h`,
      `.step-tag`, `text-transform: uppercase`, uppercase `letter-spacing:
      0.08em` on anything bigger than 10px. Remove any you find.
- [ ] Every orange pixel should be load-bearing. If it's decoration, cut it.
