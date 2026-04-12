# Design System — ssFlow

## Product Context
- **What this is:** Market simulation engine. AI personas read a market event, post, trade, and produce a price timeline.
- **Who it's for:** Finance researchers, quant traders, market analysts who read Chinese.
- **Space/industry:** Fintech, market simulation, A-shares / multi-market.
- **Project type:** Web app (Vue.js SPA) with editorial characteristics.

## Aesthetic Direction
- **Direction:** Editorial/Magazine. ssFlow reads like a financial narrative, not a trading terminal.
- **Decoration level:** Intentional. Subtle shadows on the composer, dashed borders for collapsible sections. No decorative gradients, blobs, or ornamental elements.
- **Mood:** Opening a well-designed financial magazine. Authoritative but approachable. The serif headlines signal editorial weight; the orange accent adds energy.
- **Positioning:** Deliberately anti-terminal, anti-dashboard. Where 东方财富 screams "financial portal" and Bloomberg screams "professional terminal," ssFlow says "read this story about what happened in the market."
- **Reference sites:** TradingView (data density), Stripe (design craft), Wise (bold brand color)

## Typography

Four fonts, each with a clear role. Never mix roles.

- **Display/Hero:** `Noto Serif SC` (weights 400, 500, 600) — CJK editorial authority. Used for page headlines, empty state messages, persona names, instrument names. This is the signature font.
- **Accent/Labels:** `Fraunces` italic (weights 400, 600) — Section headers, navigation labels, contextual hints, the "研究工具" disclaimer. Always italic. Pairs with the serif headlines to reinforce the editorial feel.
- **Body/UI:** `Inter` (weights 400, 500, 600) — All interface text: paragraphs, buttons, form labels, card body text. Fallbacks: -apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, Noto Sans SC, sans-serif.
- **Data/Code:** `JetBrains Mono` (weights 400, 500) — Prices, percentages, dates, tickers, stream IDs, seed values, simulation params. Always use `font-variant-numeric: tabular-nums` for column alignment.
- **Loading:** Google Fonts CDN. All four families loaded via single `<link>` tag in `index.html`.

### Type Scale
| Level | Size | Weight | Font | Usage |
|-------|------|--------|------|-------|
| h1 | 44px (desktop) / 30px (tablet) / 24px (mobile) | 500 | Noto Serif SC | Page headlines |
| h2 | 30px / 24px | 500 | Noto Serif SC | Section headlines |
| h3 | 14px | 600 | Noto Serif SC | Card titles, persona names |
| section-label | 14px | 400 | Fraunces italic | Section headers ("事件", "角色") |
| body | 15px | 400 | Inter | Default text |
| ui | 13px | 500 | Inter | Buttons, form labels |
| small | 12px | 400 | Inter | Card body, secondary info |
| caption | 11px | 400 | Inter / JetBrains Mono | Tags, badges, hints |
| micro | 10px | 400-600 | JetBrains Mono | Timestamps, counts, params |

### Anti-patterns
- Never use Noto Serif SC for body text or UI labels (too heavy for running text).
- Never use Inter for headlines (loses editorial character).
- Never use Fraunces for anything other than section labels and accents.
- Never use system fonts for data columns (breaks alignment).

## Color

- **Approach:** Restrained. One accent color + neutrals + market semantics.

### Palette
| Token | Hex | Usage |
|-------|-----|-------|
| `--ss-accent` | `#ff6a00` | Primary accent. Links, active states, highlighted verbs, CTA hover. Used sparingly. |
| `--ss-accent-soft` | `#fff4ec` | Accent backgrounds. Event-subject borders, restored file chips, tag backgrounds. |
| `--ss-good` | `#1d7a3a` | Positive market moves. Price up, success alerts, completed steps. |
| `--ss-bad` | `#c43c3c` | Negative market moves. Price down, error alerts, failures. |
| `--ss-info` | `#2c66d4` | Informational. URL chips, link badges, info-source persona accent. |
| `--ss-fg` | `#0a0a0a` | Primary text. Headlines, body text, data values. |
| `--ss-fg-muted` | `#6b6b6b` | Secondary text. Subtitles, card body, meta info. Passes WCAG AA (5.74:1). |
| `--ss-fg-faint` | `#767676` | Tertiary text. Tags, hints, timestamps, placeholders. Passes WCAG AA (4.54:1). |
| `--ss-line` | `#ececec` | Light borders. Card borders, table row separators. |
| `--ss-line-strong` | `#cfcfcf` | Strong borders. Input borders, composer border, workflow rail connector. |
| `--ss-bg` | `#ffffff` | Page background. |
| `--ss-bg-soft` | `#f7f7f7` | Soft backgrounds. Sidebar, skeleton loaders, param groups. |
| `--ss-bg-card` | `#fafafa` | Card backgrounds (when needed). |

### Dark Mode Strategy
Reduce saturation 10-20%, lighten accent slightly, invert neutral scale:
| Token | Dark Value |
|-------|------------|
| `--ss-bg` | `#111111` |
| `--ss-bg-soft` | `#1a1a1a` |
| `--ss-fg` | `#e8e8e8` |
| `--ss-fg-muted` | `#999999` |
| `--ss-fg-faint` | `#777777` |
| `--ss-line` | `#2a2a2a` |
| `--ss-line-strong` | `#3a3a3a` |
| `--ss-accent` | `#ff8533` |
| `--ss-accent-soft` | `#2a1a0a` |
| `--ss-good` | `#2ea84e` |
| `--ss-bad` | `#e05555` |
| `--ss-info` | `#5588ee` |

### Anti-patterns
- No purple/violet gradients. Orange is the identity.
- No blue as a primary accent (every fintech uses blue for "trust." ssFlow uses orange for "energy").
- Red/green for market semantics only, never decorative.

## Spacing
- **Base unit:** 4px
- **Density:** Comfortable (not compact, not spacious)
- **Scale:** `4 | 8 | 10 | 12 | 14 | 16 | 24 | 32 | 48 | 64`
- Common patterns:
  - Component internal padding: 10-14px
  - Section gaps: 24-32px
  - Page padding: 48-72px (desktop), 24-28px (mobile)
  - Card padding: 10-14px
  - Grid gaps: 10-12px

## Layout
- **Approach:** Hybrid. Sidebar rail for workflow (app-like), editorial main content (magazine-like).
- **Sidebar:** 220px fixed width (`--ss-sidebar-w`). Collapses to horizontal 56px strip at 860px.
- **Max content width:** 760px (Home), 1120px (Setup), flexible (Run/Replay/Report).
- **Breakpoints:**
  - Desktop: >860px (sidebar visible)
  - Tablet/mobile: <=860px (sidebar collapses to top strip)
  - Narrow mobile: <=560px (further type/spacing reduction)

### Border Radius Scale
| Token | Value | Usage |
|-------|-------|-------|
| `--ss-radius-sm` | `4px` | Tags, chips, badges, small inputs |
| `--ss-radius-md` | `8px` | Cards, buttons, alerts, form inputs |
| `--ss-radius-lg` | `12px` | Composer, large containers |
| `--ss-radius-pill` | `999px` | Filter pills, pill buttons |
| `--ss-radius-round` | `50%` | Status dots, workflow step indicators |

## Motion
- **Approach:** Minimal-functional. Motion aids comprehension, never decorates.
- **Signature gesture:** Rotating verb on Home headline ("发酵", "消化", "兑现", "搅动", "推演"). Slot-machine slide, 2.8s interval.
- **Pipeline:** Skeleton shimmer during loading, fade-slide-in on content arrival.
- **Easing:** enter(ease-out), exit(ease-in), move(cubic-bezier(0.32, 0.72, 0, 1))
- **Duration:** micro(50-100ms) for hover, short(150ms) for transitions, medium(300-400ms) for entrance, long(500ms) for the verb rotation.
- **Reduced motion:** All animations respect `prefers-reduced-motion: reduce`. The rotating verb interval is skipped entirely. Pipeline animations collapse to instant. Pulse animations are disabled.

## Iconography
- No icon library. Inline SVGs only (feather-style, stroke-width: 2).
- Persona type indicators: `◆` (solid diamond) for traders, `◇` (open diamond) for info sources.
- Workflow steps: numbered dots (01, 02, 03, 04) in the rail.
- No emoji as design elements.

## Accessibility
- **Touch targets:** 44px minimum height on all interactive elements.
- **Focus-visible:** 2px solid `--ss-accent`, 2px offset. Global catch-all in `global.css`.
- **Contrast:** All text tokens pass WCAG AA (4.5:1 minimum). `--ss-fg-faint` is #767676 (4.54:1 on white).
- **Reduced motion:** Respected everywhere.
- **ARIA:** Labels on all `<main>` elements, `aria-current="step"` on active workflow steps, `aria-expanded` on expandable cards.
- **Keyboard:** All interactive elements reachable via Tab. PersonaCard responds to Enter/Space.

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-12 | Initial design system created | Codified existing patterns from 15+ design polish commits. Based on /design-consultation research of TradingView, Stripe, Wise, 东方财富, Bloomberg Terminal. |
| 2026-04-12 | Noto Serif SC for CJK headlines | Deliberate departure from fintech category (which uses sans-serif). Editorial authority, visual distinction from every competitor. |
| 2026-04-12 | Orange (#ff6a00) as sole accent | Anti-blue positioning. Every fintech uses blue/purple. Orange signals energy and market movement. |
| 2026-04-12 | --ss-fg-faint darkened to #767676 | Was #888888 (3.54:1 contrast, WCAG AA fail). Fixed to 4.54:1. |
| 2026-04-12 | Border-radius scale formalized | Normalized 8 ad-hoc values (2/3/4/6/8/12/50%/999px) into 5 tokens (sm/md/lg/pill/round). |
