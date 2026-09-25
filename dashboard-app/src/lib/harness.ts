/**
 * Pure helpers for the harness dashboard views (spec 2026-09-25 v2 §4).
 * No DOM, no fetch — unit-tested in harness.test.ts.
 */

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "Sep 25 14:31 UTC" — absolute, UTC, no seconds. */
export function fmtUtc(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()} ${hh}:${mm} UTC`;
}

/** "2h ago" / "3d ago" / "just now". */
export function fmtAgo(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return '';
  const secs = (now - new Date(iso).getTime()) / 1000;
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

/** §4.2: absolute UTC first, relative second — "Sep 25 14:31 UTC · 2h ago". */
export function fmtWhen(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return '—';
  return `${fmtUtc(iso)} · ${fmtAgo(iso, now)}`;
}

/** "in 3 h 02 m" until a deadline, or "past the deadline". */
export function relUntil(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return '';
  const mins = Math.floor((new Date(iso).getTime() - now) / 60000);
  if (mins < 0) return 'past the deadline — the default applies on the next tick';
  const h = Math.floor(mins / 60);
  if (h >= 24) return `in ${Math.floor(h / 24)} d ${h % 24} h`;
  const m = String(mins % 60).padStart(2, '0');
  return h > 0 ? `in ${h} h ${m} m` : `in ${mins % 60} m`;
}

/** Deadline within 4 hours — the countdown turns amber. */
export function isSoon(iso: string | null | undefined, now: number = Date.now()): boolean {
  if (!iso) return false;
  const ms = new Date(iso).getTime() - now;
  return ms >= 0 && ms < 4 * 3600 * 1000;
}

export function recipients(keyArgs: Record<string, unknown> | null | undefined): string[] {
  const out: string[] = [];
  for (const field of ['to', 'cc']) {
    const v = keyArgs?.[field];
    if (Array.isArray(v)) out.push(...v.map(String));
    else if (typeof v === 'string' && v) out.push(v);
  }
  return out;
}

/**
 * What a ledger row acted on — only the shapes the store keeps (ledger_store
 * key_args): a path, recipients, a chat, a uuid or enum word, or a free-text
 * field reduced to `<name>_sha256` + `<name>_len`. Never the text itself.
 */
export function ledgerTarget(keyArgs: Record<string, unknown> | null | undefined, tombstone = false): string {
  if (tombstone) return 'details removed by retention';
  const a = keyArgs ?? {};
  const to = recipients(a);
  if (to.length) return `to ${to.join(', ')}`;
  if (typeof a.chat_id === 'string' || typeof a.chat_id === 'number') {
    return a.file_path ? `chat ${a.chat_id} · ${a.file_path}` : `chat ${a.chat_id}`;
  }
  if (typeof a.path === 'string') return a.path;
  if (typeof a.file_path === 'string') return a.file_path;
  for (const [k, v] of Object.entries(a)) {
    if (k.endsWith('_sha256') && typeof v === 'string') {
      const name = k.slice(0, -'_sha256'.length);
      const len = a[`${name}_len`];
      return `${name} sha256:${v.slice(0, 8)}…${len != null ? ` (${len} chars)` : ''}`;
    }
  }
  const words = Object.entries(a).filter(([, v]) => typeof v === 'string' || typeof v === 'number');
  return words.length ? words.map(([k, v]) => `${k}=${v}`).join(' · ') : '—';
}

export type Tone = 'ok' | 'warn' | 'muted';
export interface Verdict { title: string; detail: string; tone: Tone }

export interface RuleSummary {
  mode: string | null;
  first_event_at: string | null;
  by_mode: Record<string, number>;
}
export interface TopPattern { context: string | null; tool: string | null; violation: string }

/**
 * §4.3 — derived from the numbers, never stored. Absence of events is never
 * reported as "nothing would be refused": it is "not measured", "not
 * checking", or "nothing flagged since <when measuring began>".
 */
export function ruleVerdict(
  rule: RuleSummary, windowLabel: string, eventsPersisted: boolean, top: TopPattern | null,
): Verdict {
  if (!eventsPersisted) {
    return { title: 'Not measured', detail: 'Event persistence is off (NOUS_F026_PERSISTENCE_ENABLED), so nothing was recorded.', tone: 'warn' };
  }
  if (rule.mode === 'off') {
    return { title: 'Not checking', detail: 'This rule is off, so it records nothing.', tone: 'muted' };
  }
  const total = Object.values(rule.by_mode).reduce((a, b) => a + b, 0);
  const most = top ? ` Most: ${[top.context, top.tool, top.violation].filter(Boolean).join(' · ')}.` : '';
  if (rule.mode === 'enforce') {
    const refused = rule.by_mode.enforce ?? 0;
    return { title: 'Enforcing', detail: `Refused ${refused} in ${windowLabel}.${refused ? most : ''}`, tone: 'ok' };
  }
  if (total === 0) {
    // first_event_at is the rule's first flag ever, not when it was deployed:
    // with none, nothing has been flagged yet — not "nothing would be refused".
    if (!rule.first_event_at) {
      return { title: 'No flags recorded yet', detail: 'The rule is on and has flagged nothing so far.', tone: 'muted' };
    }
    return { title: `No flags in ${windowLabel}`, detail: `First flag ${fmtUtc(rule.first_event_at)}.`, tone: 'muted' };
  }
  return { title: 'Enforce would refuse calls like these', detail: `${total} in ${windowLabel}.${most}`, tone: 'warn' };
}

export interface ClaimSummary {
  mode: string | null;
  by_evidence: { exact: number; plausible: number; none: number };
  legacy: { events: number; violations: number };
  evidence_since: string | null;
}

export function claimVerdict(claims: ClaimSummary, eventsPersisted: boolean): Verdict {
  if (!eventsPersisted) {
    return { title: 'Not measured', detail: 'Event persistence is off, so no claim checks were recorded.', tone: 'warn' };
  }
  const none = claims.by_evidence.none;
  if (claims.mode === 'enforce') {
    // "queued", not "got": a one-turn session (a subtask, a heartbeat turn)
    // ends before the next turn, and end_conversation drops the correction.
    return { title: 'Enforcing', detail: `${none} claims had no evidence; a correction was queued for the next turn.`, tone: 'ok' };
  }
  return { title: claims.mode === 'off' ? 'Not checking' : 'Checking', detail: `${none} claims had no evidence and would have got a correction.`, tone: none ? 'warn' : 'muted' };
}

/** Plain-language gloss for each flag code (title attributes + legend). */
export const FLAG_GLOSS: Record<string, string> = {
  'not offered': 'The turn called a tool it was not offered in that step.',
  undeclared: 'A heartbeat check or callback used a tool it did not declare.',
  'level:external': 'A context allowed only local work reached another host or person.',
  spawn: 'A context that may not start new work tried to spawn a task.',
  reenable: 'A callback tried to turn its own check back on.',
  unclassified: 'A tool with no declared risk class was called.',
  'no evidence': 'The reply claimed an action no tool call backs up.',
};
