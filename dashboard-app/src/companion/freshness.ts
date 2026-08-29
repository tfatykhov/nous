// F092.1: pure freshness formatting for the micro-app AppHeader stamp.
// Extracted as a function of (iso, nowMs, staleAfterS) so tests hand-feed
// the clock instead of faking timers (rev-ui #10).

export interface Freshness {
  label: string;
  stale: boolean;
}

const DEFAULT_STALE_AFTER_S = 3600;

export function formatFreshness(
  composedAtIso: string,
  nowMs: number,
  staleAfterS: number = DEFAULT_STALE_AFTER_S,
): Freshness {
  const composedMs = Date.parse(composedAtIso);
  if (Number.isNaN(composedMs)) {
    // A stamp we cannot parse is worse than stale — say so, amber.
    return { label: 'composed at unknown time', stale: true };
  }
  const ageS = Math.max(0, (nowMs - composedMs) / 1000);
  const stale = ageS >= (staleAfterS > 0 ? staleAfterS : DEFAULT_STALE_AFTER_S);
  let ago: string;
  if (ageS < 60) {
    ago = 'just now';
  } else if (ageS < 3600) {
    ago = `${Math.floor(ageS / 60)}m ago`;
  } else if (ageS < 86400) {
    ago = `${Math.floor(ageS / 3600)}h ago`;
  } else {
    ago = `${Math.floor(ageS / 86400)}d ago`;
  }
  const composed = new Date(composedMs);
  const hh = String(composed.getUTCHours()).padStart(2, '0');
  const mm = String(composed.getUTCMinutes()).padStart(2, '0');
  return { label: `composed ${hh}:${mm} UTC · ${ago}`, stale };
}
