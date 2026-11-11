/// <reference types="node" />
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { markupOf, scriptBlocks, styleBlocks } from '../../lib/testing/css';

// Source contracts for the companion's components (jsdom cannot lay out or resolve var(), so these read the source;
// the screenshot round is the visual proof). Every colour comes from a token, so a theme re-skins every component;
// the paper default and the legacy themes differ only through tokens that the legacy group in companion.css holds at
// the old values.

const CATALOG = __dirname;
const SHELL = join(__dirname, '..');
const FILES = [
  ...readdirSync(CATALOG).filter((f) => f.endsWith('.svelte')).map((f) => join(CATALOG, f)),
  ...readdirSync(SHELL).filter((f) => f.endsWith('.svelte')).map((f) => join(SHELL, f)),
];
const src = (name: string) => readFileSync(join(CATALOG, `${name}.svelte`), 'utf-8');
const style = (name: string) => styleBlocks(src(name));

/** A colour literal: a hex colour, or an rgb/hsl/oklch function. */
const LITERAL = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|oklch)\(/;

describe('companion components: no colour literal (source contract)', () => {
  it('the literal pattern catches each form and spares a route and a token', () => {
    for (const bad of ['color: #fff;', 'fill="#12304f"', 'background: rgba(0, 0, 0, 0.3)', 'hsl(210 50% 40%)']) {
      expect(bad).toMatch(LITERAL);
    }
    for (const good of ["href={'#/s/' + id}", 'color: var(--accent);', 'color-mix(in srgb, var(--ink) 13%, transparent)']) {
      expect(good).not.toMatch(LITERAL);
    }
  });

  it.each(FILES.map((f) => [f.slice(SHELL.length + 1).replace(/\\/g, '/'), f]))(
    '%s: style, markup and script read colours from tokens only',
    (_, file) => {
      const source = readFileSync(file, 'utf-8');
      // markup without its route hrefs (#/s/...), which are not colours
      const markup = markupOf(source).replace(/['"]#\/[^'"]*['"]/g, '');
      for (const [part, text] of [
        ['style', styleBlocks(source)],
        ['markup', markup],
        ['script', scriptBlocks(source)],
      ]) {
        expect(text.match(LITERAL)?.[0] ?? null, `${part}`).toBeNull();
      }
    },
  );
});

describe('containers, inputs and media take the paper shape through tokens (source contract, PR 6 T5)', () => {
  it('buttons and chip rows are pills through --ctl-radius, tabs through --tablist-radius', () => {
    expect(style('ButtonView')).toMatch(/\.btn \{[^}]*border-radius: var\(--ctl-radius\)/);
    // Tabs round through --tablist-radius: a legacy tab keeps square ends, or its underline would curve.
    expect(style('TabsView')).toMatch(/button \{[^}]*border-radius: var\(--tablist-radius\)/);
    expect(style('ChipRowView')).toMatch(/\.chip \{[^}]*border-radius: var\(--ctl-radius\)/);
  });

  it('a default button is the control fill and outline (--ctl-bg, --ctl-border), with a visible focus ring', () => {
    const css = style('ButtonView');
    expect(css).toMatch(/\.btn \{[^}]*background: var\(--ctl-bg\);[^}]*border: 1px solid var\(--ctl-border\)/);
    expect(css).toMatch(/\.btn:focus-visible \{[^}]*outline: 2px solid var\(--accent\)/);
  });

  it('option rows are bordered rounded cards; the chosen one takes --select-border and its wash', () => {
    const css = style('ChoicePickerView');
    expect(css).toMatch(/\.option \{[^}]*padding: var\(--option-pad\);[^}]*border: var\(--option-border\);[^}]*border-radius: var\(--radius-md\)/);
    expect(css).toMatch(/\.option\.selected \{[^}]*border-color: var\(--select-border\);[^}]*background: var\(--option-selected-bg\)/);
    expect(css).toMatch(/\.picker \{[^}]*border: var\(--card-border\);[^}]*padding: var\(--picker-pad\)/);
  });

  it('a selected chip is filled through --chip-selected-bg and -ink; focus has its own ring, --chip-focus (review I1)', () => {
    const css = style('ChoicePickerView');
    expect(css).toMatch(
      /\.options\.chips \.option\.selected \{[^}]*border-color: var\(--accent\);[^}]*background: var\(--chip-selected-bg\);[^}]*color: var\(--chip-selected-ink\)/,
    );
    expect(css).toMatch(/\.options\.chips \.option:focus-within \{[^}]*border-color: var\(--accent\);[^}]*outline: var\(--chip-focus\);[^}]*outline-offset: 2px/);
  });

  it('a nested card and media take --radius-md; a nested card sits on --nested-bg with --card-border', () => {
    expect(style('CardView')).toMatch(/\.card \{[^}]*background: var\(--nested-bg\);[^}]*border: var\(--card-border\);[^}]*border-radius: var\(--radius-md\)/);
    expect(style('ImageView')).toMatch(/\.img \{[^}]*border-radius: var\(--radius-md\)/);
    expect(style('VideoView')).toMatch(/\.frame \{[^}]*border-radius: var\(--radius-md\)/);
  });

  it('section headings and chip labels are sentence case on paper through --label-transform', () => {
    expect(style('SectionView')).toMatch(/h3 \{[^}]*text-transform: var\(--label-transform\);[^}]*letter-spacing: var\(--label-tracking\)/);
    expect(style('ChipRowView')).toMatch(/\.l \{[^}]*letter-spacing: var\(--label-tracking\);[^}]*text-transform: var\(--label-transform\)/);
  });

  it('text fields have a field border and fill (--field-border, --field-bg)', () => {
    for (const name of ['TextFieldView', 'DateTimeInputView']) {
      expect(style(name), name).toMatch(/background: var\(--field-bg\);[^}]*border: 1px solid var\(--field-border\)/);
    }
    expect(style('ChoicePickerView')).toMatch(/\.filter \{[^}]*background: var\(--field-bg\);[^}]*border: 1px solid var\(--field-border\)/);
  });

  it('tabs: a pill track with a raised active segment on paper, the underline in a legacy theme', () => {
    const css = style('TabsView');
    expect(css).toMatch(/\.tablist \{[^}]*background: var\(--tablist-bg\);[^}]*border-bottom: var\(--tablist-rule\);[^}]*padding: var\(--tablist-pad\)/);
    expect(css).toMatch(/button\.active \{[^}]*background: var\(--tab-active-bg\);[^}]*box-shadow: var\(--tab-active-shadow\);[^}]*border-bottom-color: var\(--tab-underline\)/);
  });

  it('the modal is a card: --radius, --card-border, --card-shadow over the --scrim backdrop', () => {
    // (T6 adds its siblings below)
    const css = style('ModalView');
    expect(css).toMatch(/dialog \{[^}]*border: var\(--card-border\);[^}]*border-radius: var\(--radius\);[^}]*box-shadow: var\(--card-shadow\)/);
    expect(css).toMatch(/dialog::backdrop \{[^}]*background: var\(--scrim\)/);
  });
});

describe('tiles, cards, tables and the timeline take the paper shape through tokens (source contract, PR 6 T6)', () => {
  it('a stat tile is a soft tile; its tone rule is --tile-rule-width wide (none on paper, 3px in a legacy theme)', () => {
    const css = style('StatTileView');
    expect(css).toMatch(/\.tile \{[^}]*background: var\(--nested-bg\);[^}]*border: var\(--card-border\);[^}]*border-left: var\(--tile-rule-width\) solid var\(--border\);[^}]*border-radius: var\(--radius-md\)/);
    expect(css).toMatch(/\.label \{[^}]*text-transform: var\(--label-transform\);[^}]*letter-spacing: var\(--label-tracking\)/);
  });

  // (the ApprovalPanel is a callout since review I3: --callout-bg, tested below)
  it('metric, score, decision and review cards sit on --nested-bg with --card-border', () => {
    for (const [name, cls] of [
      ['MetricCardView', 'metric'],
      ['ScoreCardView', 'score'],
      ['DecisionCardView', 'card'],
      ['ActionReviewCardView', 'review'],
    ]) {
      expect(style(name), name).toMatch(new RegExp(`\\.${cls} \\{[^}]*background: var\\(--nested-bg\\);[^}]*border: var\\(--card-border\\)`));
    }
  });

  it('coloured badges are tinted pills on paper and outlined in a legacy theme (--badge-tint, --badge-edge)', () => {
    const tint = /background: color-mix\(in srgb, currentColor var\(--badge-tint\), transparent\)/;
    const edge = /border(?:-color)?: (?:1px solid )?color-mix\(in srgb, currentColor var\(--badge-edge\), transparent\)/;
    for (const name of ['ApprovalPanelView', 'ActionReviewCardView', 'DecisionCardView']) {
      expect(style(name), name).toMatch(tint);
      expect(style(name), name).toMatch(edge);
    }
  });

  it('footer controls are pills with the control fill and outline', () => {
    expect(style('AppFooterView')).toMatch(/\.ctl \{[^}]*background: var\(--ctl-bg\);[^}]*border: 1px solid var\(--ctl-border\);[^}]*border-radius: var\(--ctl-radius\)/);
  });

  it('the timeline marks each entry with a ringed dot on paper (--tl-dot, --tl-dot-ring), drawn, not a glyph', () => {
    const css = style('TimelineView');
    expect(css).toMatch(/li::before \{[^}]*content: '';[^}]*width: var\(--tl-dot\);[^}]*height: var\(--tl-dot\);[^}]*border: var\(--tl-dot-ring\)/);
    expect(css).toMatch(/li\.flag::before \{[^}]*border-color: var\(--warn\)/);
  });

  it('the confidence meter bands are the status marks (review I2)', () => {
    const css = style('ConfidenceMeterView');
    for (const [band, mark] of [['low', 'crit'], ['mid', 'warn'], ['high', 'ok']]) {
      expect(css, band).toMatch(new RegExp(`\\.fill\\.${band} \\{[^}]*background: var\\(--${mark}-mark\\)`));
    }
  });

  it('the confidence meter track is --meter-track (a hairline on white, the old soft fill in a legacy theme)', () => {
    // (T7 charts follow in the next describe)
    expect(style('ConfidenceMeterView')).toMatch(/\.track \{[^}]*background: var\(--meter-track\)/);
  });
});

describe('every control is a 44px tap target on paper through --tap-min (source contract, review I5)', () => {
  it.each([
    ['ButtonView', '.btn'],
    ['TabsView', 'button'],
    ['AppFooterView', '.ctl'],
    ['TextFieldView', 'textarea'],
    ['DateTimeInputView', 'input'],
    ['ChoicePickerView', '.filter'],
    ['ChoicePickerView', '.options.chips .option'],
  ])('%s %s', (name, sel) => {
    expect(style(name).split(`${sel} {`)[1].split('}')[0]).toContain('min-height: var(--tap-min)');
  });

  it('the shell (always paper) sizes its pills and the install buttons at 44px', () => {
    const shell = styleBlocks(readFileSync(join(SHELL, 'Companion.svelte'), 'utf-8'));
    expect(shell.split('.chip {')[1].split('}')[0]).toMatch(/min-height: 44px;[^}]*display: inline-flex;[^}]*align-items: center/);
    const install = styleBlocks(readFileSync(join(SHELL, 'InstallPrompt.svelte'), 'utf-8'));
    for (const sel of ['.go', '.x']) {
      expect(install.split(`${sel} {`)[1].split('}')[0], sel).toMatch(/min-height: 44px;[^}]*display: inline-flex;[^}]*align-items: center/);
    }
  });

  it('"Link" is a 44px-tall target in a strip of its own', () => {
    const shell = styleBlocks(readFileSync(join(SHELL, 'Companion.svelte'), 'utf-8'));
    expect(shell.split('.surface.linked {')[1].split('}')[0]).toContain('padding-top: 2.75rem');
    expect(shell.split('.permalink {')[1].split('}')[0]).toMatch(/top: 0;[^}]*right: 0;[^}]*height: 2\.75rem;[^}]*display: inline-flex;[^}]*align-items: center/);
  });
});

describe('no coloured side rule and no tracked capitals on paper (source contract, review I3)', () => {
  it.each([
    ['ApprovalPanelView', '.panel', 'warn'],
    ['ActionReviewCardView', '.review', 'accent'],
    ['DecisionCardView', '.card', 'accent'],
    ['SectionView', '.app-section.model', 'warn'],
  ])('%s %s: its side rule is --side-rule wide', (name, sel, ink) => {
    const rule = style(name).split(`${sel} {`)[1].split('}')[0];
    expect(rule).toContain(`border-left: var(--side-rule) solid var(--${ink})`);
    expect(rule).not.toMatch(/border-left: \d/);
  });

  it('the model-supplied section insets by --side-rule-pad; the ApprovalPanel is a callout (--callout-bg, --callout-pad)', () => {
    expect(style('SectionView').split('.app-section.model {')[1].split('}')[0]).toContain('padding-left: var(--side-rule-pad)');
    const panel = style('ApprovalPanelView').split('.panel {')[1].split('}')[0];
    expect(panel).toContain('background: var(--callout-bg)');
    expect(panel).toContain('padding: var(--callout-pad)');
  });

  it.each([
    ['ApprovalPanelView', '.badge', '0.06em'],
    ['ActionReviewCardView', '.badge', '0.06em'],
    ['DecisionCardView', '.badge', '0.04em'],
    ['SectionView', '.chip', '0.06em'],
    ['ScoreCardView', '.status', '0.09em'],
  ])('%s %s: capitals and tracking only through --label-transform and --caps', (name, sel, em) => {
    const rule = style(name).split(`${sel} {`)[1].split('}')[0];
    expect(rule).toContain('text-transform: var(--label-transform)');
    expect(rule).toContain(`letter-spacing: calc(${em} * var(--caps))`);
    expect(rule).not.toContain('uppercase');
  });

  it('the two fixed badges are written in sentence case and never wrap', () => {
    expect(markupOf(src('ApprovalPanelView'))).toContain('<span class="badge">Escalation</span>');
    expect(markupOf(src('ActionReviewCardView'))).toContain('<span class="badge">Action review</span>');
    for (const name of ['ApprovalPanelView', 'ActionReviewCardView']) {
      expect(style(name).split('.badge {')[1].split('}')[0], name).toContain('white-space: nowrap');
    }
  });

  it("a flagged timeline entry's rail is --tl-flag-rule", () => {
    expect(style('TimelineView').split('li.flag {')[1].split('}')[0]).toContain('border-left-color: var(--tl-flag-rule)');
  });
});

describe('figures are condensed through --figure-stretch (source contract, fix round I4)', () => {
  it.each([
    ['MetricCardView', '.value'],
    ['ScoreCardView', '.value'],
    ['ScoreCardView', '.rv'],
    ['SparklineView', '.cur'],
    ['SparklineView', '.single'],
    ['DeltaListView', '.d'],
    ['DeltaListView', '.r'],
    ['DataTableView', 'td.end'],
    ['AppHeaderView', '.elapsed'],
    ['StatTileView', '.value'],
  ])('%s %s', (name, sel) => {
    const at = style(name).indexOf(`${sel} {`);
    expect(at, `${name} ${sel}`).toBeGreaterThan(-1);
    expect(style(name).slice(at).split('}')[0]).toContain('font-stretch: var(--figure-stretch)');
  });

  it('words keep their width: the MetricCard pill and the ChipRow value take the figure face, not the stretch', () => {
    const pill = style('MetricCardView').split('.pill {')[1].split('}')[0];
    expect(pill).toContain('font-family: var(--font-numeric)');
    expect(pill).not.toContain('font-stretch');
    expect(style('ChipRowView').split('.v {')[1].split('}')[0]).not.toContain('font-stretch');
  });

  it("the slider's value is a figure (--font-numeric); the decision id and code stay mono", () => {
    expect(style('SliderView').split('.value {')[1].split('}')[0]).toContain('font-family: var(--font-numeric)');
    expect(style('DecisionCardView').split('.id {')[1].split('}')[0]).toContain('font-family: var(--font-mono)');
    expect(style('Text')).toMatch(/pre code \{[^}]*font-family: var\(--font-mono\)/);
  });
});

describe('charts and graphs read the paper ramp and node colours (source contract, PR 6 T7)', () => {
  it('MemoryGraph colours a node by its type token', () => {
    const script = scriptBlocks(src('MemoryGraphView'));
    for (const [type, token] of [
      ['fact', '--node-fact'],
      ['decision', '--node-decision'],
      ['episode', '--node-episode'],
      ['procedure', '--node-procedure'],
      ['chunk', '--node-chunk'],
    ]) {
      expect(script, type).toContain(`${type}: 'var(${token})'`);
    }
  });

  it('graph edges are --graph-edge at --graph-edge-opacity (a hairline is invisible on white)', () => {
    for (const name of ['MemoryGraphView', 'DagGraphView']) {
      expect(style(name), name).toMatch(/\.edge \{[^}]*stroke: var\(--graph-edge\);[^}]*stroke-opacity: var\(--graph-edge-opacity\)/);
      expect(markupOf(src(name)), name).not.toMatch(/stroke="var\(--border\)"/);
    }
  });

  it('chart lines are --line-width wide with --line-cap ends and --line-join corners', () => {
    expect(style('LineChartView')).toMatch(/\.ln \{[^}]*stroke-width: var\(--line-width\);[^}]*stroke-linecap: var\(--line-cap\);[^}]*stroke-linejoin: var\(--line-join\)/);
  });

  it('the sparkline end dot is ringed in the card colour (no area wash: F096 keeps a non-zero-based line unfilled)', () => {
    expect(style('SparkSvg')).toMatch(/\.end \{[^}]*stroke: var\(--spark-ring\)/);
  });

  it('graph boxes are nested tiles', () => {
    expect(style('DagGraphView')).toMatch(/\.dag \{[^}]*background: var\(--nested-bg\);[^}]*border: var\(--card-border\);[^}]*border-radius: var\(--radius-md\)/);
    expect(style('MemoryGraphView')).toMatch(/\.graph \{[^}]*background: var\(--nested-bg\);[^}]*border: var\(--card-border\);[^}]*border-radius: var\(--radius-md\)/);
  });
});
