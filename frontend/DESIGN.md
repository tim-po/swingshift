# Loopyard UI — design rules

The beta audience is people seeing Loopyard for the first time. The app should feel
like Notion or Figma: **calm on arrival, deeper the more you explore**. Newcomers see
a small core; power users keep every capability, one click or one setting away.

## 1. Progressive disclosure (the main idea)

- **Lead with the one thing the page is for.** Each view has one primary action
  (ember button) and one primary content block. Everything else is secondary.
- **Reveal on relevance.** A section, filter or control appears once it has
  something in it (≥2 projects → project filter; ≥6 loops → search; an issue exists →
  Issues in the sidebar). Empty power features are hidden, not shown empty.
- **Tuck, don't delete.** Advanced controls go in a `⋯` menu, a collapsed
  "Details" disclosure, or behind `useShowAll()` (from `shell/disclosure.ts`), which
  the "Show everything" switch in Help & settings turns on. Never remove a capability.
- **Explaining copy is a dismissible `<Intro id>`** (one or two plain sentences), not
  permanent prose. Tooltips (`title`) carry precise/technical detail.
- **Nothing expensive happens on disclosure.** Expanding a section or opening a
  dialog must never start an agent, session, or job — only an explicit button does.

## 2. Voice

- Plain words first. Say *machine* (not origin/device/box), *plan* (not objective/
  Idea Hub doc), *agent* / *team* (not crew/cast/role template), *turn*, *result*.
  Internal terms (origin, guardian, sweep, reconcile, rollup, framed prompt, sid,
  wind-down, Door A/B) may appear only in tooltips or behind Show everything.
- Sentence case everywhere. No UPPERCASE letter-spaced labels. No roadmap leaks
  ("lands in a later slice"), no defensive meta-copy ("capability-honest", "no fake …").
- Numbers need words: `2 workers · 1 reviewer`, not `2W · 1I · 1M`.

## 3. Visual system (tokens in `src/styles/tokens.css`)

- **Only tokens.** No raw px font sizes, hex/rgba colours, or ad-hoc spacing in view
  CSS. Type: `--fs-2xs … --fs-xl`. Space: `--sp-1 … --sp-16` (4px grid). Radius:
  `--r-xs/sm/r/lg/pill`. Surfaces: `--bg < --surface < --surface-2 < --surface-3`.
  Text: `--text / -2 / -3 / -4`. Status: `--ok / --warn / --bad / --info` (+ `-tint`, `-line`).
- **Sans for everything; mono only for things you'd copy** — ids, commit hashes,
  paths, commands, loop names in code-ish contexts (use `.mono`).
- **Ember is precious**: the one primary button per view, the active nav icon, a
  focus ring, a "new" dot. Not section labels, not chips, not borders.
- **Fewer boxes.** Prefer flat lists (`.rows` / `.row`) and whitespace over nested
  bordered cards. A card inside a card is a smell. Metadata is quiet text joined by
  `·`, not a row of pills. Use `Pill` only for a status/category that must scan.
- **Status = `Badge`** (dot + sentence-case label). One status vocabulary: the list
  and the detail page must say the same word for the same state.
- Icons: `components/icons.tsx` (`<Icon name>`). No emoji, no unicode glyph icons.
- Buttons: `Btn` / `.btn` (`primary`, default secondary, `quiet`, `danger`; `sm`/`lg`).
- Headings: page `.h1` (via `Page`), section `.section-h`, card title `.h3`.
- No nested scroll areas inside the page scroll (let content flow; paginate or
  "Show all" instead). Modals scroll inside themselves and never clip.
- Every state designed: loading (`Skeleton`), empty (friendly `Empty` with one next
  step), error (`QueryState`). Mobile ≤760px still works.

## 4. Shared pieces

`components/ui.tsx` (Page, Card, Btn, Badge, Pill, Chip, Empty, QueryState, Skeleton),
`components/Intro.tsx`, `components/icons.tsx`, `shell/disclosure.ts`
(`useShowAll`, `markSeen`), `shell/Modal.tsx`. Extend these instead of copying CSS.

## 5. Contextual tips

`components/Coachmark.tsx` — wrap a real control to point at it the first time a
user reaches it (`<Coachmark id="loop.rate" title="Rate the result" body="…">`).
One visible at a time app-wide, shown once per id, never with Show everything on.
Use sparingly: only for the few moments that teach the core loop (describe → team →
watch → result → rate), not to label every button.
