---
name: AgentOps AI
description: A light working console where every answer is a case-numbered record with a signed custody chain.
colors:
  ground: "#F2F3EF"
  paper: "#FBFBF8"
  wash: "#E9EAE4"
  white: "#FFFFFF"
  ink: "#172033"
  ink-2: "#4A5468"
  ink-3: "#5E6678"
  rule: "#C9CCC4"
  manila: "#D9B46A"
  manila-ink: "#4A3A16"
  manila-hover: "#E2C07D"
  stamp: "#5B3FA0"
  stamp-soft: "#ECE7F6"
  stamp-deep: "#45307D"
  seal: "#B4232F"
  seal-soft: "#F7E3E4"
  record: "#2A7247"
  record-soft: "#E1F0E6"
  caution: "#8A5A00"
  caution-soft: "#F6EBD3"
  info: "#2D5B8A"
  info-soft: "#E2ECF5"
typography:
  display:
    fontFamily: "Public Sans Variable, system-ui, sans-serif"
    fontSize: "30px"
    fontWeight: 800
    lineHeight: "37px"
    letterSpacing: "-0.02em"
  headline:
    fontFamily: "Public Sans Variable, system-ui, sans-serif"
    fontSize: "24px"
    fontWeight: 700
    lineHeight: "31px"
    letterSpacing: "-0.015em"
  answer:
    fontFamily: "Public Sans Variable, system-ui, sans-serif"
    fontSize: "20px"
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: "-0.005em"
  title:
    fontFamily: "Public Sans Variable, system-ui, sans-serif"
    fontSize: "17px"
    fontWeight: 700
    lineHeight: "25px"
  subtitle:
    fontFamily: "Public Sans Variable, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 600
    lineHeight: "23px"
  body:
    fontFamily: "Public Sans Variable, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: "21px"
    fontFeature: "\"ss01\", \"cv11\""
  body-sm:
    fontFamily: "Public Sans Variable, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: "19px"
  label:
    fontFamily: "Public Sans Variable, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: "17px"
  seal:
    fontFamily: "Public Sans Variable, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 800
    lineHeight: "19px"
    letterSpacing: "0.08em"
  figure:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "20px"
    fontWeight: 500
    lineHeight: 1
    fontFeature: "\"zero\", \"tnum\""
  mono-id:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "12px"
    fontWeight: 700
    lineHeight: "17px"
    fontFeature: "\"zero\", \"tnum\""
  micro:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "11px"
    fontWeight: 400
    lineHeight: "16px"
    letterSpacing: "0.02em"
rounded:
  none: "0px"
spacing:
  hair: "4px"
  xs: "6px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  2xl: "24px"
  3xl: "28px"
  page: "32px"
  gutter: "40px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.none}"
    padding: "0 14px"
    height: "36px"
  button-primary-hover:
    backgroundColor: "#25304A"
  button-primary-active:
    backgroundColor: "#0F1626"
  button-secondary:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "0 14px"
    height: "36px"
  button-secondary-hover:
    backgroundColor: "{colors.wash}"
  button-danger:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.seal}"
    rounded: "{rounded.none}"
    padding: "0 14px"
    height: "36px"
  button-danger-hover:
    backgroundColor: "{colors.seal-soft}"
  button-seal:
    backgroundColor: "{colors.stamp}"
    textColor: "{colors.white}"
    rounded: "{rounded.none}"
    padding: "0 14px"
    height: "36px"
  button-seal-hover:
    backgroundColor: "{colors.stamp-deep}"
  button-ghost:
    textColor: "{colors.ink-2}"
    rounded: "{rounded.none}"
    padding: "0 14px"
    height: "36px"
  button-ghost-hover:
    backgroundColor: "{colors.wash}"
    textColor: "{colors.ink}"
  button-sm:
    typography: "{typography.label}"
    padding: "0 10px"
    height: "28px"
  input:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.none}"
    padding: "0 12px"
    height: "36px"
  status-verified:
    backgroundColor: "{colors.record-soft}"
    textColor: "{colors.record}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "1px 6px"
  status-critical:
    backgroundColor: "{colors.seal-soft}"
    textColor: "{colors.seal}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "1px 6px"
  status-pending:
    backgroundColor: "{colors.caution-soft}"
    textColor: "{colors.caution}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "1px 6px"
  status-info:
    backgroundColor: "{colors.info-soft}"
    textColor: "{colors.info}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "1px 6px"
  status-stamp:
    backgroundColor: "{colors.stamp-soft}"
    textColor: "{colors.stamp}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "1px 6px"
  status-neutral:
    backgroundColor: "{colors.wash}"
    textColor: "{colors.ink-2}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "1px 6px"
  record-id:
    textColor: "{colors.ink}"
    typography: "{typography.mono-id}"
    rounded: "{rounded.none}"
    padding: "1px 6px"
  record-id-invert:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    typography: "{typography.mono-id}"
    rounded: "{rounded.none}"
    padding: "1px 6px"
  exhibit-chip:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    typography: "{typography.mono-id}"
    rounded: "{rounded.none}"
    padding: "0 6px"
  exhibit-chip-hover:
    backgroundColor: "{colors.stamp}"
  exhibit:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "8px 12px 12px"
  rail:
    backgroundColor: "{colors.manila}"
    textColor: "{colors.ink}"
    width: "232px"
  nav-item:
    textColor: "{colors.ink}"
    typography: "{typography.subtitle}"
    rounded: "{rounded.none}"
    padding: "0 10px"
    height: "36px"
  nav-item-hover:
    backgroundColor: "{colors.manila-hover}"
  nav-item-active:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
  nav-badge:
    backgroundColor: "{colors.stamp}"
    textColor: "{colors.white}"
    typography: "{typography.mono-id}"
    padding: "0 6px"
    height: "20px"
  custody-chain:
    backgroundColor: "{colors.wash}"
    padding: "16px 24px"
  sheet:
    backgroundColor: "{colors.ground}"
    width: "560px"
---

# Design System: AgentOps AI

## Overview

**Creative North Star: "The Custody Record"**

The console reads as a forensic file, not a chat app. Every run is a case with a record number, its workflow steps are hands in a custody chain (step, signer, time, tokens) signed one after another on a pale wash band, and the chain closes on a tilted stamp seal carrying the outcome. Approvals are a human countersignature on that same chain. The page is bond-white paper ruled in carbon ink; the shell is a manila folder edge; stamp-violet is the ink of seals, signatures, focus, and the one action that commits.

Density is working density: 13–14px body, tabular mono for every ID and figure, ruled sections rather than cards, square corners everywhere. Hierarchy is carried by type weight and 1.5px ink rules, not by boxes or tinted panels. Colour is evidentiary: state colours (seal-red, record-green, caution-amber, info-blue) appear only where a status means something, always paired with the status word.

The build refines the contract's "rules instead of boxes and shadows" into a narrower truth: sections are ruled, but things you can open or decide on (exhibits, approval records, side sheets) are framed objects, and exactly two shadows exist, both tied to state or overlay.

**Key Characteristics:**
- Manila rail (232px) as the committed shell colour; bond-white ground for all work.
- 1.5px carbon-ink rules under every section heading, page header, and decision footer.
- Zero border radius on every component.
- JetBrains Mono with tabular, slashed-zero figures for IDs, timings, tokens, and counts.
- The custody chain: a horizontal strip of signed hands ending in a rotated stamp seal.
- Focus-and-dim: the focused exhibit or hand stays at full ink; the rest drop to 40–45% opacity.

## Colors

A cool, papery neutral field with carbon ink, one committed shell colour (manila), one signing colour (stamp-violet), and four strictly semantic state colours, each with a soft tint for its chip.

### Primary
- **Stamp Violet** (stamp): seals for supported answers, the countersign button, the approval count badge, link text to other records, focus outlines, caret, the live-progress pulse, and an exhibit's focused border. Deep variant (stamp-deep) is its hover and the selection text; soft variant (stamp-soft) is selection background and the "approved" chip.

### Secondary
- **Manila Folder** (manila): the navigation rail and nothing else. Manila-hover lightens a nav item on hover; manila-ink is the rail's quiet text (tagline, counts, connection state).

### Tertiary (state colours)
- **Seal Red** (seal / seal-soft): critical, refused, failed, rejected, timeout, budget exhausted. Also the danger button and error boxes. A hand that errored gets a seal-red left rule.
- **Record Green** (record / record-soft): answered, ready, verified, passed. Also the "API ready" square.
- **Caution Amber** (caution / caution-soft): pending, processing, awaiting approval, insufficient evidence, suspected drift. Also the "Held for your countersignature" notice.
- **Notice Blue** (info / info-soft): insufficient data and neutral notices ("New file: it does not exist yet").

### Neutral
- **Bond Ground** (ground): the page, sticky ask bar, sheet body, approval records.
- **Record Paper** (paper): raised record surfaces: exhibits, inputs, code blocks, secondary buttons, and text on ink.
- **Chain Wash** (wash): the custody-chain band, decision footers, hover on rows and ghost buttons, neutral chips.
- **Carbon Ink** (ink): primary text, 1.5px rules, the primary button, the active nav item, inverted record IDs and exhibit chips.
- **Ink 2** (ink-2): secondary text, metadata, table headers, figure labels.
- **Ink 3** (ink-3): tertiary text, step numbers, placeholders, and icons. Tuned to pass 4.5:1 on every surface it sits on (5.17:1 on ground, 4.76:1 on wash, 5.55:1 on paper); anything that must read first steps up to ink-2.
- **Hairline** (rule): 1px dividers between rows, table head rules, code-block borders, scrollbar thumb.

### Named Rules
**The Evidence-Only Colour Rule.** Seal, record, caution and info appear only to state a status the API returned. They are never decoration, never a section accent.

**The Never-Colour-Alone Rule.** Every status shows a square swatch and the status word together.

**The Violet Signs Rule.** Stamp-violet means signed, sealed, focused, or "commit this". If an element neither signs nor commits, it is not violet.

## Typography

**Display Font:** Public Sans Variable (with system-ui, sans-serif)
**Body Font:** Public Sans Variable, with stylistic sets ss01 and cv11
**Label/Mono Font:** JetBrains Mono (400/500/700), with tabular and slashed-zero figures and contextual alternates off

**Character:** a sober civic grotesque for everything a person reads, and an engineer's monospace for everything a machine recorded. The split is semantic: if it is an ID, a time, a token count, a model name, or a tool name, it is mono.

### Hierarchy
The scale is one fixed ladder for the whole console: 11 / 12 / 13 / 14 / 15 / 17 / 20 / 24 / 30px.
- **Display** (800, 30px/37px, -0.02em): screen titles in the page head, and the empty Ask state.
- **Headline** (700, 24px/31px, -0.015em): the case question beside its record number.
- **Answer** (400, 20px, 1.55, -0.005em): the agent's answer, capped at 68ch.
- **Title** (700, 17px/25px): ruled section headings and sheet titles.
- **Subtitle** (600, 15px/23px): nav items, list subheads ("Exhibits", "Earlier this session"), empty-state titles.
- **Body** (400, 14px/21px): default; **Body small** (13px/19px) for tables, inputs, buttons, exhibit snippets.
- **Label** (600, 12px/17px): status chips, table heads, figure captions, small buttons.
- **Seal** (800, 13px, 0.08em, uppercase): only inside stamp seals.
- **Figure** (mono 500, 20px, line-height 1): measured numbers in figure rows.
- **Mono ID** (mono 700, 12px): record numbers (EX-, AP-, EV-) and exhibit labels (S1, T1).
- **Micro** (11px/16px, 0.02em): step ordinals, request IDs, "sealed 43.0 s".

### Named Rules
**The Machine-Writes-Mono Rule.** IDs, durations, token counts, costs, model and tool names are JetBrains Mono with tabular figures. Prose is never mono.

**The Uppercase-Is-A-Stamp Rule.** Uppercase with tracking exists only inside seals. Headings, labels and buttons stay sentence case.

## Layout

A two-column shell: a fixed 232px manila rail and a scrolling main column. Below the md breakpoint (768px) the rail collapses into a top bar: wordmark and a "Connection" disclosure on one row, then a horizontally scrolling nav with a right-edge fade mask; the active item is scrolled into view.

Screens use a 32px horizontal page inset with 32px top/bottom padding, inside per-screen max widths chosen for content (Approvals 1024px, Documents 1152px, Workspace and Evals 1400px; Ask and Executions run full width). The page head sits 28px above content. Ruled sections put 16px between rule and content.

The Ask case is header, full-bleed custody chain, then a work area with a 40px column gap: answer on the left, a 380px exhibits column on the right from xl (1280px). Figures sit in a five-column row (two columns below sm) under a hairline. The ask box is a sticky footer ruled in ink. List-detail screens (Executions, Evals, Workspace) use a fixed left list column (300–340px) beside a fluid detail.

Spacing steps in 4px multiples: 4, 6, 8, 12, 16, 20, 24, 28, 32, 40.

## Elevation & Depth

Flat by default. Depth comes from tone (ground, paper, wash) and from ink rules, not from shadow. Two shadows exist, both state- or overlay-bound. Overlays dim the page with a 20% ink scrim.

### Shadow Vocabulary
- **Lift** (`box-shadow: 0 1px 2px rgba(23,32,51,.06), 0 6px 18px -6px rgba(23,32,51,.18)`): only on the focused exhibit, together with its stamp border.
- **Sheet** (`box-shadow: -12px 0 40px -12px rgba(23,32,51,.28)`): the right-hand side sheet over the scrim.

### Named Rules
**The Lift-Means-Focus Rule.** Nothing casts a shadow at rest. Lift appears only on the exhibit currently in focus.

## Shapes

Square everywhere: zero radius on buttons, inputs, chips, exhibits, sheets, status swatches and the live pulse (the only rounded thing is the browser-drawn scrollbar thumb). Borders come in three weights: 1px hairline (rule colour) for row dividers and inner splits; 1.5px for section rules, inputs, frames and secondary buttons; 2px for stamp seals. Seals are the one rotated form, tilted -2deg. Empty states use a 1.5px dashed hairline frame.

## Components

### Buttons
Blunt, square, filled with ink or with intent.
- **Shape:** square (0px), 36px tall with 14px side padding; small size 28px tall, 10px padding, 12px label.
- **Primary:** carbon-ink fill, paper text, semibold 13px. Used for Ask and routine submit.
- **Seal:** stamp-violet fill, white text, often with a stamp icon. Reserved for countersigning and the action that commits a held run.
- **Secondary:** paper fill with 1.5px ink border; hover goes to wash.
- **Danger:** paper fill, 1.5px seal-red border and seal-red text; hover seal-soft. Reject and Stop.
- **Ghost:** ink-2 text, hover to wash and full ink.
- **Hover / Focus:** 150ms colour transition on the standard ease-out; focus is the global 2px stamp-violet outline at 2px offset. Disabled drops to 45% opacity; busy shows a spinner and disables.

### Chips (status marks)
- **Style:** soft tint background, the state colour as text, a 6px square swatch, semibold 12px, 1px by 6px padding, no border, no radius.
- **State:** tone is derived from the API status word (verified, critical, pending, info, stamp for approved, neutral otherwise).

### Record IDs and exhibit chips
- **Record ID:** mono 12px bold, a 1px ink-at-30% frame; inverted (ink fill, paper text) when it heads the current case or a pending approval. Format is prefix plus four hex characters (EX-9A16).
- **Exhibit chip:** inline in the answer, ink fill, paper mono label (S4, T1), turns stamp-violet on hover; hovering or focusing it focuses the matching exhibit.

### Cards / Containers
- **Ruled panel (default):** no box. A 17px bold title on a 1.5px ink bottom rule, optional ink-2 meta and right-aligned actions.
- **Exhibit:** paper fill, 1.5px border at ink 25% (full ink on hover, stamp-violet with Lift when focused), a hairline-ruled header with the ink ID chip, filename and an arrow icon, then location and a 4-line clamped snippet.
- **Approval record:** ground fill, 1.5px ink frame while pending (hairline once decided), hairline header, an embedded compact custody chain, and a wash footer ruled in ink holding the reason input, Reject and Countersign.
- **Error box:** seal-soft fill, 1px seal border at 35%, alert icon, message plus a recovery hint.

### Inputs / Fields
- **Style:** paper fill, 1.5px border at ink 40%, 36px tall, 13px text, square.
- **Hover / Focus:** border to ink 70% on hover; stamp-violet border on focus, with a violet caret.
- **Segmented control (graph / loop):** 1.5px ink frame; the selected segment is ink-filled.

### Navigation
- **Rail:** manila ground, wordmark in 20px extrabold over a manila-ink tagline, a 1.5px ink rule beneath.
- **Items:** 36px tall, 15px semibold, 16px icon; hover manila-hover; active is ink-filled with paper text. Counts sit right in mono manila-ink; the pending-approvals count is a stamp-violet badge.
- **Connection foot:** ink-ruled, a state square (record-green ready, seal-red unreachable), tenant ID in mono, and a key field joined to an ink "Use" button.

### Custody Chain (signature component)
A wash band, 16px vertical padding (12px compact), holding an ordered strip of hands. Each hand: a zero-padded mono ordinal and step name, the signers in mono bold (model and tool names, "×n" for repeats, "you · time" when a human signed), then duration and tokens in mono. Hands are separated by a 1.5px ink left rule, seal-red when that hand errored or was refused; a pending hand reads "awaiting you" in caution. The live hand shows a pulsing violet square. When it overflows, the hidden edge fades out over 40px. The strip ends, past a hairline, in the seal: a 2px bordered box tilted -2deg, uppercase outcome over a mono "sealed n s", violet for supported or answered, caution for held or no evidence, seal-red for anything else. The same chain is reused on Executions and inside approval records.

### Side Sheet
Right-aligned, up to 560px, ground fill, 1.5px ink left edge, Sheet shadow, over a 20% ink scrim. Header ruled in ink; Escape closes; focus goes to the close button.

### Motion
New records and hands enter with "sign" (4px rise and fade, 420ms); seals land with "stamp" (scale from 1.35 to 1 with a blur clearing, 520ms); sheets slide in 24px (320ms). All use the ease-out curve cubic-bezier(0.16, 1, 0.3, 1). The live pulse cycles opacity every 1.4s. Reduced motion collapses all durations to 1ms.

## Do's and Don'ts

### Do:
- **Do** separate sections with a 1.5px carbon-ink rule under a bold title; reserve framed boxes for objects a person opens or decides on.
- **Do** keep every corner square (0px).
- **Do** set every ID, timing, token count and cost in JetBrains Mono with tabular figures.
- **Do** pair each status colour with its swatch and status word.
- **Do** reuse the custody chain wherever a run's steps are shown, ending in its seal.
- **Do** use focus-and-dim for linked evidence: the focused item at full ink, the rest at 40–45% opacity.
- **Do** keep stamp-violet for seals, signatures, focus and the commit action.

### Don't:
- **Don't** round corners or soften a component into a pill.
- **Don't** cast a shadow on anything at rest; Lift is for the focused exhibit and Sheet for the side sheet only.
- **Don't** use seal-red, record-green, caution or info as decoration or section accents.
- **Don't** set uppercase tracked text anywhere but inside a seal.
- **Don't** put manila anywhere but the shell rail.
- **Don't** set prose, headings or buttons in the monospace.
