import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';
import VideoView from './VideoView.svelte';
import { registry } from './index';
import { store } from '../store.svelte';

// Render-level companion to video.test.ts: the classifier's verdict reaches
// the DOM as exactly one media element (or a quiet placeholder), every src
// comes from the verdict rather than the raw url, and the iframe attributes
// are the fixed ones.

const SURFACE = 'video-test-surface';
const YT_EMBED = 'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ';
// Exact, so an added capability (allow-forms, camera, autoplay…) fails a test.
const SANDBOX = 'allow-scripts allow-same-origin allow-presentation allow-popups allow-popups-to-escape-sandbox';
const ALLOW = 'encrypted-media; picture-in-picture; fullscreen';

function renderVideo(props: Record<string, unknown>, dataModel: Record<string, unknown> = {}) {
  store.reset();
  store.apply(null, {
    version: 'v1.0',
    createSurface: { surfaceId: SURFACE, catalogId: 'basic', components: [], dataModel },
  } as never);
  return render(VideoView, {
    props: { surfaceId: SURFACE, comp: { id: 'v', component: 'Video', ...props } },
  });
}

describe('VideoView', () => {
  beforeEach(() => store.reset());

  it('renders a <video> for an overlay file', () => {
    const { container } = renderVideo({ url: '/dashboard/v2/trip/a.mp4', posterUrl: '/dashboard/v2/trip/p.jpg' });
    const videos = container.querySelectorAll('video');
    expect(videos).toHaveLength(1);
    const video = videos[0];
    expect(video.getAttribute('src')).toBe('/dashboard/v2/trip/a.mp4');
    expect(video.getAttribute('poster')).toBe('/dashboard/v2/trip/p.jpg');
    expect(video.hasAttribute('controls')).toBe(true);
    expect(video.hasAttribute('playsinline')).toBe(true);
    expect(video.getAttribute('preload')).toBe('metadata');
    expect(video.hasAttribute('autoplay')).toBe(false);
    expect(video.getAttribute('aria-label')).toBe('Video');
    expect(container.querySelector('iframe')).toBeNull();
  });

  it('renders a <video> for an https mp4', () => {
    const { container } = renderVideo({ url: 'https://cdn.example.com/v/clip.mp4' });
    const video = container.querySelector('video')!;
    expect(video.getAttribute('src')).toBe('https://cdn.example.com/v/clip.mp4');
    expect(video.hasAttribute('poster')).toBe(false);
  });

  it('drops a poster that fails the url check', () => {
    const { container } = renderVideo({ url: '/dashboard/v2/trip/a.mp4', posterUrl: 'javascript:alert(1)' });
    expect(container.querySelector('video')!.hasAttribute('poster')).toBe(false);
  });

  it.each([
    'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
    'https://youtu.be/dQw4w9WgXcQ',
    'https://www.youtube.com/shorts/dQw4w9WgXcQ',
  ])('YouTube watch, youtu.be and shorts all render the nocookie iframe (%s)', (url) => {
    const { container } = renderVideo({ url });
    const frames = container.querySelectorAll('iframe');
    expect(frames).toHaveLength(1);
    const frame = frames[0];
    expect(frame.getAttribute('src')).toBe(YT_EMBED);
    expect(frame.getAttribute('referrerpolicy')).toBe('strict-origin-when-cross-origin');
    expect(frame.getAttribute('sandbox')).toBe(SANDBOX);
    expect(frame.getAttribute('allow')).toBe(ALLOW);
    expect(container.querySelector('video')).toBeNull();
  });

  it('Vimeo renders the player iframe', () => {
    const { container } = renderVideo({ url: 'https://vimeo.com/76979871' });
    const frame = container.querySelector('iframe')!;
    expect(frame.getAttribute('src')).toBe('https://player.vimeo.com/video/76979871?dnt=1');
    expect(frame.getAttribute('sandbox')).toBe(SANDBOX);
    expect(frame.getAttribute('allow')).toBe(ALLOW);
    expect(frame.getAttribute('referrerpolicy')).toBe('strict-origin-when-cross-origin');
  });

  it.each([
    'javascript:alert(1)',
    'http://youtu.be/dQw4w9WgXcQ',
    'data:video/mp4;base64,AA',
    'https://evil.com/x',
    '/dashboard/v2/../a2ui/x.mp4',
  ])('unsafe or unknown urls render no media element (%s)', (url) => {
    const { container } = renderVideo({ url });
    expect(container.querySelector('video, iframe, source')).toBeNull();
    expect(container.textContent).toContain('Video unavailable');
    expect(container.textContent).not.toContain(url);
  });

  it('empty url renders nothing', () => {
    const { container } = renderVideo({ url: '' });
    expect(container.textContent).toBe('');
    expect(container.querySelector('video, iframe, .ph')).toBeNull();
  });

  it('a bound url resolves from the data model', () => {
    const embed = renderVideo({ url: { path: '/clip' } }, { clip: 'https://youtu.be/dQw4w9WgXcQ' });
    expect(embed.container.querySelector('iframe')!.getAttribute('src')).toBe(YT_EMBED);
    embed.unmount();

    const unsafe = renderVideo({ url: { path: '/clip' } }, { clip: 'javascript:1' });
    expect(unsafe.container.querySelector('video, iframe')).toBeNull();
    expect(unsafe.container.textContent).toContain('Video unavailable');
  });

  it('a unregistered-fallback guard: Video is registered, AudioPlayer is not', () => {
    expect(registry.Video).toBeDefined();
    expect(registry.AudioPlayer).toBeUndefined();
  });
});
