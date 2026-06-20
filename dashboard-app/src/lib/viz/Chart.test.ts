import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import Chart from './Chart.svelte';

describe('Chart.svelte', () => {
  it('constructs a Chart on mount and destroys on unmount', async () => {
    const destroy = vi.fn();
    // vi.fn with a regular function (not arrow) so `new Chart(...)` works as a constructor.
    const ctor = vi.fn(function () {
      return { destroy, update: vi.fn(), data: {}, options: {} };
    });
    vi.stubGlobal('Chart', ctor);
    const { unmount } = render(Chart, { props: { type: 'line', data: { labels: [], datasets: [] }, options: {} } });
    expect(ctor).toHaveBeenCalledTimes(1);
    unmount();
    expect(destroy).toHaveBeenCalledTimes(1);
  });
});
