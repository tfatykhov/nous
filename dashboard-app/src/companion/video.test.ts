import { describe, it, expect } from 'vitest';
import { classifyVideoUrl, isSafePosterUrl } from './video';

// video.ts is the whole security surface of the Video component: a `url` may
// be a data-model binding the server never sees, so this classifier is the
// only gate between an agent-supplied string and a <video src> or <iframe src>.

const YT_EMBED = 'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ';
const VIMEO_EMBED = 'https://player.vimeo.com/video/76979871?dnt=1';

const YOUTUBE = [
  'https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s',
  'https://youtube.com/watch?v=dQw4w9WgXcQ',
  'https://m.youtube.com/watch?v=dQw4w9WgXcQ',
  'https://youtu.be/dQw4w9WgXcQ',
  'https://youtu.be/dQw4w9WgXcQ?si=abc',
  'https://www.youtube.com/shorts/dQw4w9WgXcQ',
];

const VIMEO = ['https://vimeo.com/76979871', 'https://www.vimeo.com/76979871/'];

const REJECTED: unknown[] = [
  'http://youtu.be/dQw4w9WgXcQ',
  'javascript:alert(1)',
  'data:video/mp4;base64,AAAA',
  'blob:https://x/1',
  '//youtu.be/dQw4w9WgXcQ',
  'https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ',
  'https://evil.com/watch?v=dQw4w9WgXcQ',
  'https://user:pw@youtu.be/dQw4w9WgXcQ',
  'https://www.youtube.com/watch?v=short',
  'https://www.youtube.com/watch?v=dQw4w9WgXcQ%22%3E',
  'https://www.youtube.com/watch?v=dQw4w9WgXcQ"onload="x',
  'https://www.youtube.com/embed/dQw4w9WgXcQ',
  'https://vimeo.com/76979871/abcdef1234',
  'https://vimeo.com/channels/staffpicks',
  'https://example.com/page',
  'https://example.com/clip.mov',
  '/dashboard/v2/../a2ui/x.mp4',
  '/dashboard/v2/%2e%2e/x.mp4',
  '/a2ui/x.mp4',
  '/dashboard/v2/trip/a.html',
  '',
  42,
  null,
  'https://cdn.example.com/' + 'a'.repeat(3000) + '.mp4',
  // Additions beyond the plan's table. A browser strips tab/CR/LF from a URL
  // before parsing it, so `.\t.` would become a `..` segment after our check.
  '/dashboard/v2/.\t./a2ui/x.mp4',
  '/dashboard/v2/trip/a\n.mp4',
  // A malformed escape must classify as nothing, not throw.
  '/dashboard/v2/%zz/x.mp4',
  // An encoded slash hides a traversal inside one segment.
  '/dashboard/v2/a%2F..%2F..%2Fa2ui%2Fx.mp4',
];

describe('classifyVideoUrl', () => {
  it('an overlay file is a <video> with the url unchanged', () => {
    expect(classifyVideoUrl('/dashboard/v2/trip/a.mp4')).toEqual({ kind: 'video', src: '/dashboard/v2/trip/a.mp4' });
    expect(classifyVideoUrl('/dashboard/v2/trip/a.webm?x=1')).toEqual({
      kind: 'video',
      src: '/dashboard/v2/trip/a.webm?x=1',
    });
  });

  it('a direct https file is a <video>, extension case-insensitive', () => {
    expect(classifyVideoUrl('https://cdn.example.com/v/clip.MP4')).toEqual({
      kind: 'video',
      src: 'https://cdn.example.com/v/clip.MP4',
    });
  });

  it.each(YOUTUBE)('YouTube %s is the built nocookie embed', (raw) => {
    expect(classifyVideoUrl(raw)).toEqual({ kind: 'embed', provider: 'youtube', src: YT_EMBED });
  });

  it.each(VIMEO)('Vimeo %s is the built player embed', (raw) => {
    expect(classifyVideoUrl(raw)).toEqual({ kind: 'embed', provider: 'vimeo', src: VIMEO_EMBED });
  });

  it.each(REJECTED.map((raw) => [raw]))('%j is nothing', (raw) => {
    expect(classifyVideoUrl(raw)).toEqual({ kind: 'none' });
  });

  it('every embed points at the provider origin and ends in a well-formed id', () => {
    for (const raw of [...YOUTUBE, ...VIMEO]) {
      const result = classifyVideoUrl(raw);
      if (result.kind !== 'embed') throw new Error(`${raw} did not embed`);
      const src = new URL(result.src);
      const tail = src.pathname.split('/').pop() ?? '';
      if (result.provider === 'youtube') {
        expect(src.origin).toBe('https://www.youtube-nocookie.com');
        expect(tail).toMatch(/^[A-Za-z0-9_-]{11}$/);
      } else {
        expect(src.origin).toBe('https://player.vimeo.com');
        expect(tail).toMatch(/^[0-9]{1,12}$/);
      }
    }
  });
});

describe('isSafePosterUrl', () => {
  it.each(['https://a/b.jpg', '/dashboard/v2/trip/p.png'])('%s is safe', (raw) => {
    expect(isSafePosterUrl(raw)).toBe(true);
  });

  it.each(['http://a/b.jpg', 'javascript:1', 'data:image/png;base64,AA', '/dashboard/v2/../x'])(
    '%s is not safe',
    (raw) => {
      expect(isSafePosterUrl(raw)).toBe(false);
    },
  );
});
