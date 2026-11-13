---
name: Nous dashboard and companion
description: The agent's mind made visible in plain language, on chart paper with white rounded cards and an Atlantic-blue rail.
colors:
  chart-paper: "#eef2f3"
  card-white: "#ffffff"
  wash: "#f4f7f8"
  rule: "#e3e9ec"
  ink: "#14202b"
  ink-muted: "#56687a"
  atlantic: "#12304f"
  atlantic-hover: "#1a3d62"
  link-blue: "#2346c8"
  link-wash: "#e6ecfb"
  field-rule: "#7a8896"
  control-rule: "#c9d4da"
  rail-ink: "#dbe6ef"
  rail-muted: "#9fb6c9"
  rail-waiting: "#c4b5fd"
  rail-unknown: "#f9a8d4"
  frame-conversation: "#55697a"
  frame-question: "#2346c8"
  frame-task: "#0b6e7a"
  frame-decision: "#7b3fa0"
  frame-debug: "#b4531c"
  frame-creative: "#a3266f"
  frame-initiation: "#8f5c0f"
  frame-other: "#14202b"
  status-ok-fill: "#2f8a4e"
  status-warn-fill: "#b7791f"
  status-error-fill: "#d2352b"
  ok-text: "#23703d"
  ok-tint: "#e3f2e8"
  warn-text: "#8a5a0e"
  warn-tint: "#f8efdc"
  error-text: "#b02a22"
  error-tint: "#fbe9e8"
  info-text: "#1b6a9c"
  info-tint: "#e3ecf3"
  waiting: "#7b3fa0"
  waiting-tint: "#efe3f6"
  unknown: "#a3266f"
  outcome-success: "#3f8772"
  ok-mark: "#21694b"
  warn-mark: "#b4771e"
  lane-memory: "#3a6ea5"
  lane-live: "#946213"
  lane-workspace: "#5f6b76"
  lane-acts: "#96406b"
  series-0: "#274455"
  series-1: "#1f5fa8"
  series-2: "#159a7a"
  series-3: "#a8408f"
  series-4: "#d0620d"
  series-5: "#7a8a1a"
  series-6: "#985c50"
  series-7: "#4f5f6f"
  series-8: "#1c2f55"
  series-9: "#0e7490"
  series-10: "#8b5cf6"
  series-11: "#6b7280"
  series-12: "#908870"
  series-13: "#7a3f3f"
  node-fact: "#159a7a"
  node-episode: "#1f5fa8"
  node-decision: "#802c66"
  node-procedure: "#d0620d"
  node-chunk: "#8b5cf6"
  node-other: "#4f5f6f"
typography:
  headline:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "30px"
    fontWeight: 650
    lineHeight: 1.15
    letterSpacing: "-0.022em"
  title-lg:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 650
    lineHeight: 1.2
  title:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 650
    lineHeight: 1.5
  figure:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 700
    lineHeight: 1.2
    fontFeature: "'tnum'"
  figure-condensed:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 650
    lineHeight: 1.5
    fontFeature: "'tnum'"
    fontVariation: "'wdth' 82"
  body:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
  body-reading:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.5
  data:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.35
    fontFeature: "'tnum'"
  label:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 500
    lineHeight: 1.35
    letterSpacing: "0em"
  label-small:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "0.6875rem"
    fontWeight: 600
    lineHeight: 1.5
    letterSpacing: "0em"
  mono:
    fontFamily: "Red Hat Mono, ui-monospace, monospace"
    fontSize: "0.8125rem"
    fontWeight: 500
    lineHeight: 1.5
  mono-block:
    fontFamily: "Red Hat Mono, ui-monospace, monospace"
    fontSize: "0.78125rem"
    fontWeight: 400
    lineHeight: 1.5
rounded:
  sm: "8px"
  nav: "10px"
  card-sm: "12px"
  card: "18px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  2xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.atlantic}"
    textColor: "{colors.card-white}"
    typography: "{typography.body}"
    rounded: "{rounded.pill}"
    padding: "0 16px"
    height: "36px"
  button-primary-hover:
    backgroundColor: "{colors.atlantic-hover}"
    textColor: "{colors.card-white}"
  button-outline:
    backgroundColor: "{colors.card-white}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.pill}"
    padding: "0 16px"
    height: "36px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
  button-ghost-hover:
    backgroundColor: "{colors.wash}"
    textColor: "{colors.ink}"
  card:
    backgroundColor: "{colors.card-white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.card}"
    padding: "18px 20px 12px"
  stat-tile:
    backgroundColor: "{colors.card-white}"
    textColor: "{colors.ink}"
    typography: "{typography.figure}"
    rounded: "{rounded.card-sm}"
    padding: "16px"
  stat-tile-flat:
    backgroundColor: "{colors.wash}"
    textColor: "{colors.ink}"
    typography: "{typography.figure}"
    rounded: "{rounded.card-sm}"
    padding: "16px"
  status-pill-neutral:
    backgroundColor: "{colors.wash}"
    textColor: "{colors.ink-muted}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "0 8px"
    height: "20px"
  status-pill-ok:
    backgroundColor: "{colors.ok-tint}"
    textColor: "{colors.ok-text}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "0 8px"
    height: "20px"
  status-pill-warn:
    backgroundColor: "{colors.warn-tint}"
    textColor: "{colors.warn-text}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "0 8px"
    height: "20px"
  status-pill-error:
    backgroundColor: "{colors.error-tint}"
    textColor: "{colors.error-text}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "0 8px"
    height: "20px"
  status-pill-info:
    backgroundColor: "{colors.info-tint}"
    textColor: "{colors.info-text}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "0 8px"
    height: "20px"
  nav-link:
    backgroundColor: "{colors.atlantic}"
    textColor: "{colors.rail-ink}"
    typography: "{typography.body}"
    rounded: "{rounded.nav}"
    padding: "8px 12px"
  nav-link-hover:
    backgroundColor: "{colors.atlantic-hover}"
    textColor: "{colors.rail-ink}"
  nav-link-active:
    backgroundColor: "{colors.chart-paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.nav}"
  tab:
    backgroundColor: "transparent"
    textColor: "{colors.ink-muted}"
    typography: "{typography.body}"
    rounded: "{rounded.pill}"
    padding: "8px 14px"
  tab-selected:
    backgroundColor: "{colors.card-white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "8px 14px"
  table-cell:
    backgroundColor: "{colors.card-white}"
    textColor: "{colors.ink}"
    typography: "{typography.data}"
    padding: "7px 12px"
  table-header:
    backgroundColor: "{colors.card-white}"
    textColor: "{colors.ink-muted}"
    typography: "{typography.label}"
    padding: "7px 12px"
  code-block:
    backgroundColor: "{colors.wash}"
    textColor: "{colors.ink}"
    typography: "{typography.mono-block}"
    rounded: "{rounded.sm}"
    padding: "5px 10px"
  field:
    backgroundColor: "{colors.card-white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "7px 10px"
    height: "44px"
---

# Design System: Nous dashboard and companion

## Overview

**Creative North Star: "The Agent's Mind, Visible"**

Two surfaces share one world. The dashboard is a nineteen-screen reading room for one expert reader; the companion is a phone-first column of cards the agent composes. Both sit on a cool chart-paper ground with white rounded cards, deep Atlantic-blue navigation, and dark ink. There is one look and no look switch.

Meaning is carried by plain words and colour. A turn is tinted with the colour of the frame the agent was in, the frame is named in a word, and a frame change is said in one sentence with its reason. A status is a word in a tinted pill. Nothing is encoded in an invented symbol.

The finish is smooth and rounded: pill controls, soft single shadows, round handles and dots. Dense data stays dense inside that finish: tables keep 13px text in rows of about 32px, numbers align in tabular figures, and the Overview keeps its timeline compact, below the cards that say what needs the reader.

**Key Characteristics:**
- Chart-paper ground, white cards at an 18px radius with one soft shadow, Atlantic-blue rail.
- One colour per cognitive frame, shown as a 7% tint and as the frame's word.
- Status as a word in a tinted pill; a status colour never stands alone.
- Two typefaces: Archivo for everything read, Red Hat Mono for code and tool input and output.
- Dense tables and compact charts inside rounded cards.
- Every colour is a token, and guard tests in the source tree pin the rules.

## Colors

A cool, low-chroma paper and ink, one deep navy for structure and action, and small amounts of saturated colour that each mean one thing.

### Primary
- **Atlantic Blue** (`atlantic`, #12304f): the rail (the dashboard sidebar, its mobile header and drawer, the companion's top bar) and every filled action. White text sits on it. Its hover is **Atlantic Lift** (`atlantic-hover`), which is also the rail's hovered item.
- **Rail Ink** (`rail-ink`) and **Rail Mute** (`rail-muted`): item text and group headings on the rail. The current item is a chart-paper pill with ink text.

### Secondary
- **Link Blue** (`link-blue`, #2346c8): links, the focus ring on paper, and the question frame. It fills nothing but the skip link. **Link Wash** (`link-wash`) is its pale tint.

### Tertiary
- **Frame colours** (`frame-conversation`, `frame-question`, `frame-task`, `frame-decision`, `frame-debug`, `frame-creative`, `frame-initiation`): one per cognitive frame. `frame-other` is the neutral ink for a frame id outside the seven, so it never passes for conversation.
- **Status fills** (`status-ok-fill`, `status-warn-fill`, `status-error-fill`): marks only, such as a dot, a bar, or the underline below a timeline bar.
- **Status text and tints** (`ok-text` on `ok-tint`, `warn-text` on `warn-tint`, `error-text` on `error-tint`, `info-text` on `info-tint`): the pill pairs. `waiting` and `unknown` mark the two things that need the owner (a question waiting, a send in doubt); on the rail they lighten to `rail-waiting` and `rail-unknown`.
- **Categorical families**: chart series (`series-0` to `series-13`; `series-0` is the mark of a one-series chart), graph node types (`node-*`), tool lanes (`lane-*`), and, in the stylesheet only, relation types, retrieval dispositions and the context sections of a model call. Task statuses and outcomes have their own text tones and marks there too. The stylesheet `dashboard-app/src/app.css` is the authority for the families not listed in the frontmatter.
- **Companion marks** (`ok-mark`, `warn-mark`): a status tone drawn as a chart line, swatch or meter band in the companion, tuned to stay apart under red-green colour blindness.

### Neutral
- **Chart Paper** (`chart-paper`, #eef2f3): the page ground on both surfaces.
- **Card White** (`card-white`): cards, fields, the selected pill.
- **Wash** (`wash`): hovered rows, flat tiles inside a card, code and result blocks, the track behind a pill group.
- **Rule** (`rule`): 1px row dividers, chart grids, tracks.
- **Ink** (`ink`, #14202b) and **Muted Ink** (`ink-muted`, #56687a): text and secondary text.
- **Field Rule** (`field-rule`): the boundary of a text field, 3:1 on the card and the page. **Control Rule** (`control-rule`): the border of a companion default button.

### Named Rules
**The Frame Colour Rule.** The frame tokens are read by the Work trace and the Overview timeline only: as the 7% tint of a turn's frame column, as the colour of the frame's word, as the key dots, as the timeline bars, and as the short bar between two turns that runs from the old frame's colour to the new one. Screens whose data records no frame stay untinted.

**The Word With Colour Rule.** A status is never colour alone. A word says the state, and the colour agrees with it.

**The Marks And Text Rule.** A status fill is a mark; status text uses the text tone on its tint. `hexGuard.test.ts` fails a component that sets a status fill as a text colour.

**The Atlantic Action Rule.** Every filled action is Atlantic Blue. The saturated blue is left to links, focus and the question frame.

**The Separate Families Rule.** No chart series, graph node, relation or retrieval disposition colour is a frame colour or an ok, warn or error fill or text tone, and each stays a measured distance from those status tones. `tokens.test.ts` pins this, with the contrast of each colour on the surfaces it is drawn on and the distance between colours one chart draws together under normal vision, protanopia and deuteranopia.

**The Tokens Only Rule.** No colour literal, named colour or palette utility class appears in a component. `hexGuard.test.ts` enforces it, `tokenUse.test.ts` checks that every token read is declared and every token declared is read, and `oldPalette.test.ts` and `flipResidue.test.ts` keep the retired dark palette and its switch out.

## Typography

**Display Font:** Archivo (with system-ui, sans-serif)
**Body Font:** Archivo (with system-ui, sans-serif)
**Label/Mono Font:** Red Hat Mono (with ui-monospace, monospace)

**Character:** One grotesque with a width axis does all the reading, set a little tight at title size and condensed for figures; a quiet mono marks what the machine wrote. Both faces load from Google Fonts.

### Hierarchy
- **Headline** (650, 30px, 1.15, -0.022em): the page title, one per screen.
- **Title large** (650, 1.0625rem, 1.2): a group heading between card rows, and the frame's name in a trace column.
- **Title** (650, 0.9375rem): a card's heading.
- **Figure** (700, 1.5rem, 1.2, tabular): the value of a stat tile.
- **Figure condensed** (650, 0.9375rem, Archivo at 82% width, tabular): readouts, durations, table totals, hour labels.
- **Body** (400, 0.875rem, 1.5): prose in cards. **Body reading** (0.9375rem, at most 72ch) is the size of a message, a tool-call reason and a reply in the trace; the companion's body is 15px.
- **Data** (400, 0.8125rem, 1.35, tabular): table cells, chips, meta pills.
- **Label** (500 to 600, 0.75rem, no tracking, sentence case): table headers, tile labels, pills, chart text. **Label small** (600, 0.6875rem): rail group headings and count badges.
- **Mono** (500, 0.8125rem) for a tool's name; **Mono block** (400, 0.78125rem, 1.5) for tool input and output.

### Named Rules
**The Two Faces Rule.** Archivo and Red Hat Mono are the only faces of this world. Mono is for code, ids, and tool input and output, never for figures or labels.

**The Tabular Figures Rule.** The body sets tabular figures, so numbers align wherever they stack. A number column is right-aligned.

**The Sentence Case Rule.** Labels, headers and headings are sentence case. Labels carry no letter-spacing, and there are no tracked capitals.

**The One Timestamp Rule.** A timestamp reads "Nov 10 09:31 UTC": absolute, UTC, no seconds. Its day form is "Nov 10", a chart's hour is "Nov 10 15:00", and a heading's day is "10 November". Browser-locale dates and hand-cut date strings are refused by `timeFormat.test.ts`, which holds the `handMadeDates` check. A relative age ("2h ago") may follow the absolute time, and a turn in the trace shows its clock time alone ("09:31:07", UTC).

## Layout

**Dashboard.** A fixed 220px rail on the left and one scrolling column of cards on the paper, padded 24px. Cards sit 16px to 20px apart; inside a card, rows are separated by 1px rules, not by boxes. The spacing rhythm is 4, 8, 12, 16, 20 and 24px.

**The page header** has one order on every screen: the sub-navigation (when the screen has sub views) above the title; the title block on the left; "updated" and then Refresh at the right. The header wraps, so on a phone the pair drops under the title. `headerOrder.test.ts` pins it.

**Overview** opens with three cards (Needs you, Today so far, Health) and puts the day's timeline below them, compact: the timeline never leads the page.

**Tiles** fill equal columns: all in one row up to five, then rows as even as they go, so no tile is left alone on a row. Tiles inside a card are flat.

**The Work trace** gives each turn a 168px frame column beside its rows; a 300px card beside the trace drops below it at 1180px.

**Phone width.** At 768px and below the rail becomes a 52px Atlantic top bar with a drawer (at most 280px or 85% of the width), and sub-navigation links and filter pills grow to 44px tall. At 640px and below: list tables stack into bordered rows of label and value; wide numeric tables scroll in their own box with the first column fixed and a fade on the right edge; tiles go two up and an odd last tile spans the row; the frame column lies flat above its rows. `phoneTables.test.ts` pins the table and tile behaviour.

**Companion.** One column at most 720px wide, 16px between cards, under an Atlantic top bar that stays in place while the page scrolls. With more than one live surface there is a list of them: under 900px a drawer (at most 280px or 85% of the width) opened by the word Menu in the bar (the page behind it is inert until it closes), and from 900px a permanent 240px Atlantic sidebar under the bar (900px, not the dashboard's 768px, so the reading column beside it is never under 660px). The first screen shows what Nous raised in full and the composed apps as one card of rows; a lone surface is shown in full. Every control is at least 44px tall.

### Named Rules
**The Dense Stays Dense Rule.** The shared table is 13px text in rows of about 32px. Rounding the cards does not loosen the data inside them.

**The Compact Timeline Rule.** The Overview timeline is compact and sits below the Needs you, Today so far and Health cards. It never leads the page.

## Elevation & Depth

Depth is one soft, cool-tinted shadow under white cards on the paper, and tonal layering inside them. There are two elevation shadows in the system, and nothing blurs or stacks.

### Shadow Vocabulary
- **Card** (`box-shadow: 0 1px 2px rgba(18, 48, 79, 0.06), 0 6px 20px rgba(18, 48, 79, 0.07)`): every card on the paper, stat tiles on the paper, the meta pills under a title, and an outline button on hover.
- **Raised pill** (`box-shadow: 0 1px 3px rgba(18, 48, 79, 0.12)`): the selected option of a pill group.

### Named Rules
**The No Shadow On Shadow Rule.** A tile or card inside a card is a flat wash at the 12px radius, never a second shadow.

**The Plain Scrim Rule.** A sheet's backdrop is a flat scrim with no blur (`browserSurfaces.test.ts`).

## Shapes

Everything is rounded, on a short scale. Cards are 18px. Nested cards, tiles, a turn's frame column and stacked table rows are 12px. Fields, code and result blocks and notices are 8px; a rail item is 10px. Controls, tabs, chips and status pills are full pills. Dots, the replay handle and legend marks are circles. Chart bars have a 4px radius and no outline; chart lines in the companion are 2px with round ends.

Cards have no visible border; the shadow separates them from the paper. Borders are 1px rules in the rule colour, and a text field's boundary is the darker field rule. A callout or alert is a tinted block at the world's radius; no block carries a coloured side stripe (`stripes.test.ts`). The only side borders wider than 1px are neutral 2px axes that a timeline's dots or a tree's branches sit on.

## Components

### Buttons
Calm pills; one filled colour.
- **Shape:** full pill (999px). Dashboard heights are 24, 28, 32 and 36px; the page-header button is 36px with 16px side padding and grows to 44px at phone width. Companion buttons are at least 44px tall.
- **Primary:** Atlantic Blue with white text, 14px medium. Used for the action that changes something (an answer, an acknowledgement, a confirmed choice). On hover the companion button takes Atlantic Lift and the dashboard button drops to 90% strength.
- **Outline:** white with a 1px rule border and ink text; hover darkens the border and adds the card shadow. Refresh in the page header is this variant.
- **Ghost:** no fill; a wash on hover. **Destructive:** the error tint with error text, not a red fill.
- **Focus:** a 2px ring, offset 2px: Link Blue on the dashboard's paper, Atlantic in the companion, white on the rail. Disabled is 50% opacity.

### Status pills and badges
- **Style:** 20px high, 8px side padding, 12px medium text, full pill, a tint and its text, no outline. Variants: ok, warn, error, info, neutral.
- **Rule:** the pill always holds the word for the state. The Badge component and the `status-pill` class draw the same pill.

### Cards and tiles
- **Card:** white, 18px radius, the card shadow, no border, padding about 18px by 20px. A heading at Title size, then rows divided by 1px rules.
- **Stat tile:** 12px radius, 16px padding; the figure, then a 12px label, then an optional 12px note. On the paper it is white with the card shadow; inside a card it is a flat wash.
- **Meta pills:** the facts under a page title (counts, the time) are small white pills with the card shadow; an error or gated count takes its status tint.

### Inputs and fields
- **Text field:** white, 1px field-rule border, 8px radius; focus is a 2px ring. In the companion it is at least 44px tall and its placeholder is muted ink. A search box or select that sits in a filter row is a full pill, like the controls beside it.
- **Select:** the native arrow is replaced by one drawn from tokens. **Checkbox, radio and range slider:** the Atlantic accent colour.
- **Choice picker (companion):** each option is a bordered 12px card; the chosen one takes an Atlantic outline and a pale wash. As chips, the chosen chip is Atlantic with a white word.

### Navigation
- **Rail:** Atlantic ground, the product name at 21px bold, groups under 11px headings that are text, not links. An item is 14px medium on a 10px radius; hover lifts the ground; the current item is a chart-paper pill with ink text at weight 600. A count that needs the owner is a small round badge, and the count is part of the link's accessible name.
- **Sub-navigation and pill groups:** plain muted words; hover is a wash; the selected one is the white raised pill with ink text at weight 600. A pill group that sits on a white card gets a wash track so the white pill shows. `selectedPill.test.ts` pins the treatment on the sub-navigation, the Work filters, the shared filter bar, Retrieval's filters and the companion's surface switcher.
- **Icons:** the menu, close and disclosure controls are small inline line drawings with accessible names. They are affordances, not carriers of meaning.

### Data table
- **Style:** 13px cells with 7px by 12px padding and a 1px rule under each row; 12px semibold muted headers in sentence case; a wash on the hovered row. Number columns are right-aligned in tabular figures and time columns never wrap.
- **Detail:** an expandable row has a real button with a chevron that turns when open.
- **Phone:** see Layout.

### Frame column and frame-change sentence (signature)
Each turn in the Work trace is a row of two parts. The left column (168px, 12px radius) is tinted with 7% of the turn's frame colour and holds the time, the frame's name in the frame colour, how it was chosen, the turn's numbers, and the critic's verdict as a small pill. The right part lists the message, each tool call (lane, the reason first, the tool's name in mono, its status, its duration, and the result in a soft mono block), and the reply. Between two turns whose frames differ, one sentence says the change and why, beside a short bar that runs from the old frame's colour to the new one. A missing frame reads "No frame recorded"; a missing reason reads "reason not recorded". Nothing is hidden or faked: it is marked.

### Replay control
One range control per screen steps through the loaded turns or the day. The track is a 4px rule; the handle is an 18px Atlantic circle with a white ring; the readout beside it is a condensed figure. On the trace the control is sticky, and the turn it points at takes a wash on its rows only, never under the frame tint.

### Day timeline
A compact plot of the day's turns in labelled lanes, each turn a small rounded bar in its frame colour, with a red or amber underline for an error or a gated call and a legend that names every colour in words. Hours run along the bottom in condensed figures; the part of the day that has not happened is a wash labelled "not yet".

### Charts
Chart text is 12px Archivo in muted ink on a rule-coloured grid. Legends are 8px dots beside words. Bars are rounded and unoutlined. Day and hour labels stay level and skip rather than tilt. Canvas charts resolve their colours from the same tokens as the page.

The Graph screen draws memory as one mass in three dimensions (`lib/viz/BrainGraph.svelte`, on 3d-force-graph, loaded only on that screen): memories are bodies in their `--gv-*` type colour, sized by their links and paler with age; links are thin curved fibres in `--gv-other` that fade with distance into the card's surface. A tapped memory sends activation along its links, one step and then a second: small ink (`--text`) marks travel them, and those links take their `--rel-*` relation colour. Memory text appears as a hover label and in the detail sheet, never drawn on the canvas.

### Companion
- **Top bar:** Atlantic, with the product name and the connection state as a word on a light pill. On a phone with a list, Menu sits first, with the number of surfaces Nous raised as a round badge, and an open surface's name takes the product name's place.
- **Surface list:** the rail's treatment: Home, then "From Nous" and "Apps" under 11px headings, 14px medium items on a 10px radius, hover lifts the ground, and the current one is the white raised pill. "Close all apps" sits at the foot; armed, it turns to the light pill and its words change.
- **Surface:** a white 18px card with the card shadow and about 18px padding; nested cards and evidence tiles are a wash at 12px with no shadow.
- **Tabs:** pill segments on a wash track, the active one raised.
- **Legacy themes:** the companion also carries six legacy themes kept unchanged (`nous-dark`, `alpine-dusk`, `harbor`, `paper`, `signal`, `report`). They are not part of this world, and nothing in this document describes them.
- **Parity:** the companion keeps its own stylesheet; `companionParity.test.ts` fails when a value it shares with the dashboard drifts.

### Motion
State changes take 0.2s on a decelerating curve (`cubic-bezier(0.16, 1, 0.3, 1)`): a hover fill, a chevron's turn, a replayed bar's fade. Nothing moves to decorate. The one standing exception is the Graph screen: its three-dimensional memory graph turns slowly, because the turn is what makes its depth readable; the turn has its own switch, holds still under a mouse pointer, and is off under reduced motion. The only loops are small waiting signs, such as the pulse of the "loading" word. Reduced motion turns every transition off and stops every loop.

## Do's and Don'ts

### Do:
- **Do** say a state in a word and let the colour agree: "Waiting", "Clear", "4 errors".
- **Do** read every colour from a token, and add a new one to the stylesheet with its contrast and distance pinned in `tokens.test.ts`.
- **Do** use the white raised pill for the selected option of any switch, segmented control or filter group.
- **Do** keep the page header in its one order: sub-navigation, title, then "updated" and Refresh at the right.
- **Do** print a timestamp as "Nov 10 09:31 UTC", through the shared formatters.
- **Do** keep tables at 13px in rows of about 32px, with numbers right-aligned in tabular figures.
- **Do** set figures in condensed Archivo and tool input and output in Red Hat Mono.
- **Do** put a tile or card that sits inside a card on a flat wash at the 12px radius.
- **Do** make every companion control at least 44px tall, and stack list tables at phone width while wide numeric tables scroll with a fixed first column and an edge fade.
- **Do** mark what is missing in words ("No frame recorded", "not yet") rather than leaving it out.

### Don't:
- **Don't** invent a symbol or use a glyph to carry meaning: no arrows, geometric shapes, dingbats or emoji in what a screen shows. `glyphs.test.ts` fails them; punctuation such as the middle dot, dashes and the ellipsis stays.
- **Don't** show a status by colour alone.
- **Don't** read a frame token outside the Work trace and the Overview timeline, or tint a screen whose data records no frame.
- **Don't** reuse a frame colour or an ok, warn or error tone for a chart series, a graph node type, a relation or a retrieval disposition.
- **Don't** fill a control with the saturated blue; filled actions are Atlantic.
- **Don't** set a status fill as a text colour; use the text tone on its tint.
- **Don't** write a colour literal, a named colour or a palette utility class in a component.
- **Don't** bring back the look switch or a value from the retired dark palette.
- **Don't** put a coloured side stripe on a callout, alert or list row.
- **Don't** set labels in tracked capitals or Title Case.
- **Don't** let the Overview timeline lead or dominate its page.
- **Don't** format a date with the browser's locale or local-time getters, or print a cut ISO date.
- **Don't** describe or restyle the companion's legacy themes as part of this world.
