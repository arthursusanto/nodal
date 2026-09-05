# Handoff: NODAL — Allocate screen, design directions 2b "Console 74" & 2c "Foundry"

## Overview
NODAL is an industry-neutral **industrial logistics network optimizer**. The core loop: an operator selects a shipment from a queue → the solver filters infeasible facilities via hard constraints → scores the feasible ones against configurable objectives (cost / lateness / utilization / transfers) → the operator reviews the explained result on a network map and commits. These two mocks show the **Allocate** home screen in its post-solve state for shipment SHP-2214 (Port Houston → Memphis selected at score 87.4; Atlanta/Newark/Chicago rejected with coded reasons R-102/R-201/R-305).

Two approved visual directions of the same screen are bundled:
- **2b — "Console 74"** (`2b-console-74.html`): warm charcoal + amber ops console. Queue left, map center, decision panel right, live event ticker bottom.
- **2c — "Foundry"** (`2c-foundry.html`): sodium-orange heavy-industry console with an **explicit 4-stage solve pipeline** rendered as a stepper strip.

## ⚠️ About the Design Files — VIBES ONLY
These files are **design references created in HTML** — mood-accurate mockups, **not production code and not a finalized product spec**.

- **What IS the deliverable:** the visual language — palette, typography, panel chrome, borders, density, map treatment, chip/badge/button styling, the overall "industrial control console, not military" tone.
- **What is NOT final:** every tab name, button, label, workflow, data field, panel arrangement, and piece of copy is a **placeholder subject to change** as the product develops. Do not treat the information architecture as locked. Do not ship or copy this HTML.
- **The one structural idea to preserve and emphasize (from 2c): the explicit staged solve pipeline.** See next section.

Recreate these designs in the target codebase's environment and component patterns (React/Vue/etc., or the best framework choice if greenfield). Fresh implementation, styled to match.

## The Solve Pipeline (keep this — core UX pattern)
2c renders the allocation workflow as a persistent horizontal stepper directly under the app header — four equal-width segments separated by 1px borders:

```
01 ✓ SELECT — SHP-2214 · PORT HOU   |   02 ✓ FEASIBILITY — 6 → 3 PASS   |   03 ✓ SOLVE — MEM 87.4 · 41S   |   04 ▸ COMMIT — AWAITING OPERATOR
```

- Completed stages: number + ✓ in success green `#7dd87d`, description in muted mono.
- Active/pending stage: highlighted segment background (`#2b1c0d`), number + ▸ in accent orange, description in light amber.
- The stepper is **state, not decoration**: it mirrors where the selected shipment is in the select → feasibility-filter → solve → commit loop, and each stage's summary carries live data (counts, chosen facility, solve time).
- Intended behavior (subject to change): pre-solve, stages 02–04 are pending; SOLVE running shows progress; clicking a completed stage jumps to its detail (e.g. stage 02 opens the feasibility register with rejection reasons).
- This pattern should survive whichever visual direction wins — it can be restyled into 2b's chrome trivially.

## Fidelity
**High-fidelity for visual language** (colors, type, spacing, chrome are exact — recreate faithfully). **Low-fidelity for IA/content** (tabs, panels, fields, copy are indicative). When the two conflict, visual language wins; structure is negotiable.

## Screen anatomy (shared by both, 1380px design width)
Top → bottom:
1. **App header** (~52–54px): logo mark + NODAL wordmark, tab row (ALLOCATE active · NETWORK · SIMULATE · REPLAY [· BENCHMARKS]), right-aligned clock/solver status and a bordered disruption counter chip.
2. **Status strip** — 2b: one-line network KPIs (facilities, active shipments, on-time %, util, Δ vs greedy). 2c: the 01–04 **solve pipeline stepper** (see above).
3. **Main row** (~520–566px), three columns:
   - **Queue** (left, 270–288px): shipment cards; selected card gets 3px accent left-border + tinted background + status badge (SOLVED). Other states: QUEUED, HELD, TRANSIT. 2c stacks a compact disruption board at the queue's bottom; 2b puts a dashed "SELECT SHIPMENT → SOLVE" affordance there.
   - **Map** (center, flex): dark geo-schematic — CSS grid/dot background, SVG route arcs (`viewBox 0 0 1000 520`, `preserveAspectRatio="none"`), absolutely-positioned facility nodes at true relative lat/lon (%). Node grammar: origin = outlined diamond; selected = filled accent square/glow + inverted label chip; feasible alt = accent-outlined square; rejected = dark red-filled square + reason code; closed = dashed outline + hazard hatch. Route grammar: selected = solid 2.5–3px accent; feasible alts = long-dash accent at ~50% opacity; rejected = fine red dots; ambient lanes = 1px dark neutral. Overlays: layer toggle chips (top-left), region/live tag (top-right), forecast-impact strip (2c, bottom).
   - **Decision / solve result** (right, 300–324px): winner block (name + big score + cost/distance/risk/util grid, tinted bg), alternates with thin score bars, REJECTED list as `R-xxx` code + reason line, objective-mix weight bar (cost .45 / late .30 / util .15 / xfer .10), benchmark bars vs GREEDY/NEAREST (2c), pinned footer with primary COMMIT + secondary (OVERRIDE / WHAT-IF / RE-SOLVE) buttons.
4. **Event ticker** (26–30px): single-line live feed, `HH:MM:SS EVENT` items separated by dim `▪`; warnings tinted orange.

## Design tokens

### 2b — Console 74 (warm charcoal + amber)
- Backgrounds: page `#141312` · header `#191713` · right panel `#171511` · map `#100e0c` · selected row `#1f1a12` · winner block `#1c1710` · ticker `#12100e`
- Borders: primary `#2e2a24` · row hairline `#221f1a` · map lines `#332e26` · dashed affordance `#4a4438`
- Text: primary `#d9d4c9` · secondary `#b5af9f` · muted `#8d8778` · faint `#6b6355`
- Accent: **amber `#ffb000`** (active tab, selected route/node, score, COMMIT btn — text on amber is `#141312`)
- Alerts: reject red `#ff5c47` (+ light `#ff8a72`) · warning orange `#ff8a5c` · success green `#7dd87d` · reject node fill `#3a1c14`
- Type: **Barlow Semi Condensed** 600–800 for headings/labels/buttons (letter-spacing .12em on UI labels); **JetBrains Mono** 400–700 for all data, timestamps, codes, micro-labels (8.5–10px, letter-spacing .12–.18em on section headers)
- Radius: 0 everywhere. Shadows: none except selected-node glow `0 0 16px rgba(255,176,0,.45)`.
- Map bg: radial dot grid `rgba(217,212,201,.09)` 1px @ 26px + line grid `rgba(217,212,201,.04)` @ 104px.

### 2c — Foundry (sodium orange, heavy industry)
- Backgrounds: page `#17120d` · header `#1d160f` · right panel `#140f0a` · map `#0f0c08` · active stage/tab `#2b1c0d` · selected row `#241809` · winner block `#1c1207` · alarm rows `#1f1206`
- Borders: primary `#3a2c1c` · row hairline `#241b12` · map lines `#3d3020`
- Text: primary `#e3d9c9` · secondary `#c4b8a4` · muted `#998a74` · faint `#6e6250`
- Accents: **orange `#ff7a1a`** (primary: active tab/stage, selected route/node, scores, COMMIT — text on orange `#17120d`) · amber `#ffc46b` (feasible-alt outlines, soft highlights) · `#ffb000` (medium-severity alarm bars)
- Alerts: `#ff6b3d` (+ light `#ff9d7a`) · success `#7dd87d` · reject node fill `#4a1f12`
- Type: same pairing as 2b (Barlow Semi Condensed + JetBrains Mono)
- Radius: 4px on the outer card only; 0 inside. Hazard texture: `repeating-linear-gradient(45deg, rgba(255,122,26,.06) 0 9px, transparent 9px 18px)` (ticker bg); logo plate uses solid stripe variant.
- Map bg: line grid `rgba(255,122,26,.05)` @ 46px.

### Shared scales
- Spacing: 2/4-px base; panel padding 10–14px; section gaps 6–12px.
- Font sizes: micro-labels 8–9.5px caps mono · data 9.5–11px mono · row titles 11.5–13.5px semi-cond 600–700 · scores 15–16px mono 700 · wordmark 16–17px 800, letter-spacing .16–.2em.
- Min hit target for real implementation: 44px (mock buttons are visually smaller; pad hit areas).

## Interactions & behavior (intended — all subject to change)
- Click queue card → selects shipment, map shows origin + destination-so-far only.
- SOLVE (or auto-solve on select) → feasibility filter runs, then candidates/rejects populate map + decision panel; pipeline stepper advances (2c).
- Hover a rejected facility/row → tooltip with binding constraint detail.
- COMMIT → writes an auditable allocation event, queue advances; ticker prepends the event.
- Layer toggle chips switch map overlays (flows / capacity / disruptions / forecast).
- Ticker is live-updating; disruption countdowns tick (e.g. `27:29 REM`).
- No animations were designed yet; suggest restrained ones only (route draw-in on solve, ticker slide).

## State (minimum for this screen)
`shipments[]` (id, origin, reqs, status: queued|solving|solved|held|transit), `selectedShipmentId`, `solveResult` (candidates[] with score+cost+distance+risk+utilDelta, rejected[] with constraintCode+reason, objectiveWeights, solveTime, solveId), `facilities[]` (code, lat/lon, util, status: ok|full|closed), `disruptions[]`, `events[]`, `pipelineStage` (select|feasibility|solve|commit).

## Assets
No images/icons — everything is CSS/SVG primitives. Fonts from Google Fonts: Barlow Semi Condensed (500–800), JetBrains Mono (400–700). Logo marks are placeholder geometry (2b: dot–line–dot; 2c: bordered square + dot); a real mark is TBD.

## Files
- `2b-console-74.html` — Console 74 direction, self-contained, open in any browser.
- `2c-foundry.html` — Foundry direction (with the solve-pipeline stepper), self-contained.
- Each file carries a "DESIGN REFERENCE ONLY" banner note at the top of the page.
