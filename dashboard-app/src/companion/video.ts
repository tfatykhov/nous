// Video url classifier: decides what a Video component's `url` may become —
// a <video> source, an iframe embed built from a parsed id, or nothing. This
// is the whole security surface of the component: `url` is a DynamicString
// that may be a data-model binding the server never sees, so the renderer is
// the only place it can be checked. VideoView only renders the verdict.
//
// Accepted (anything else is `none`):
//   - overlay file: root-relative /dashboard/v2/<path>.mp4|.webm, no `//`,
//     no `\`, no `.`/`..` segment (plain or percent-encoded);
//   - direct file: https:, no userinfo, path ending .mp4|.webm, any host;
//   - YouTube watch?v= / shorts / youtu.be and Vimeo /<digits> links, whose
//     embed src is built from constants plus the matched id — no other part
//     of the input reaches the iframe.

export type VideoSource =
  | { kind: 'video'; src: string }
  | { kind: 'embed'; provider: 'youtube' | 'vimeo'; src: string }
  | { kind: 'none' };

const MAX_LENGTH = 2048;
const OVERLAY_PREFIX = '/dashboard/v2/';
const VIDEO_EXTENSION = /\.(mp4|webm)$/;
const YOUTUBE_ID = /^[A-Za-z0-9_-]{11}$/;
const VIMEO_ID = /^[0-9]{1,12}$/;
const YOUTUBE_HOSTS = new Set(['www.youtube.com', 'youtube.com', 'm.youtube.com']);
const VIMEO_HOSTS = new Set(['vimeo.com', 'www.vimeo.com']);
const YOUTUBE_EMBED = 'https://www.youtube-nocookie.com/embed/';
const VIMEO_EMBED = 'https://player.vimeo.com/video/';
// A browser strips tab/CR/LF from a URL before parsing it, so `.\t.` would
// turn into a `..` segment after the checks below; refuse all of them.
// eslint-disable-next-line no-control-regex
const WHITESPACE_OR_CONTROL = /[\s\u0000-\u001f\u007f]/;
const NONE: VideoSource = { kind: 'none' };

function cleaned(raw: unknown): string | null {
  if (typeof raw !== 'string') return null;
  const s = raw.trim();
  if (s.length === 0 || s.length > MAX_LENGTH || WHITESPACE_OR_CONTROL.test(s)) return null;
  return s;
}

/** The path (query/fragment removed) of a safe overlay url, else null. */
function overlayPath(s: string): string | null {
  if (!s.startsWith(OVERLAY_PREFIX) || s.includes('//') || s.includes('\\')) return null;
  const path = s.split(/[?#]/, 1)[0];
  for (const segment of path.split('/')) {
    let decoded: string;
    try {
      decoded = decodeURIComponent(segment);
    } catch {
      return null;
    }
    if (decoded === '.' || decoded === '..' || decoded.includes('/') || decoded.includes('\\')) return null;
  }
  return path;
}

function httpsUrl(s: string): URL | null {
  let url: URL;
  try {
    url = new URL(s);
  } catch {
    return null;
  }
  if (url.protocol !== 'https:' || url.username !== '' || url.password !== '') return null;
  return url;
}

/** Path segments after the leading `/`, one trailing slash tolerated. */
function segments(url: URL): string[] {
  const path = url.pathname.length > 1 && url.pathname.endsWith('/') ? url.pathname.slice(0, -1) : url.pathname;
  return path.split('/').slice(1);
}

function youtube(id: string | null | undefined): VideoSource {
  return id && YOUTUBE_ID.test(id) ? { kind: 'embed', provider: 'youtube', src: YOUTUBE_EMBED + id } : NONE;
}

export function classifyVideoUrl(raw: unknown): VideoSource {
  const s = cleaned(raw);
  if (s === null) return NONE;
  if (s.startsWith('/')) {
    const path = overlayPath(s);
    return path !== null && VIDEO_EXTENSION.test(path.toLowerCase()) ? { kind: 'video', src: s } : NONE;
  }
  const url = httpsUrl(s);
  if (url === null) return NONE;
  const parts = segments(url);
  if (YOUTUBE_HOSTS.has(url.hostname)) {
    if (parts.length === 1 && parts[0] === 'watch') return youtube(url.searchParams.get('v'));
    if (parts.length === 2 && parts[0] === 'shorts') return youtube(parts[1]);
    return NONE;
  }
  if (url.hostname === 'youtu.be') return parts.length === 1 ? youtube(parts[0]) : NONE;
  if (VIMEO_HOSTS.has(url.hostname)) {
    return parts.length === 1 && VIMEO_ID.test(parts[0])
      ? { kind: 'embed', provider: 'vimeo', src: `${VIMEO_EMBED}${parts[0]}?dnt=1` }
      : NONE;
  }
  return VIDEO_EXTENSION.test(url.pathname.toLowerCase()) ? { kind: 'video', src: url.href } : NONE;
}

/** A poster may be any https url or a safe overlay path; no extension rule. */
export function isSafePosterUrl(raw: unknown): boolean {
  const s = cleaned(raw);
  if (s === null) return false;
  return s.startsWith('/') ? overlayPath(s) !== null : httpsUrl(s) !== null;
}
