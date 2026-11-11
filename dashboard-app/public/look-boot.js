// The dashboard's look, set on <html> before the app loads so the page never flashes the other look. The same rule
// as resolveLook() in src/lib/look.ts (look.test.ts runs both on one table): ?look=paper|dark wins and is
// remembered, else the remembered look, else dark (no attribute). Storage can throw (private mode): the URL still
// applies.
(function () {
  try {
    var key = 'nous.look';
    var look = null;
    var fromUrl = new URLSearchParams(location.search).get('look');
    if (fromUrl === 'paper' || fromUrl === 'dark') {
      look = fromUrl;
      try { localStorage.setItem(key, look); } catch (e) { /* not remembered */ }
    } else {
      try {
        var stored = localStorage.getItem(key);
        if (stored === 'paper' || stored === 'dark') look = stored;
      } catch (e) { /* no memory: dark */ }
    }
    if (look === 'paper') document.documentElement.setAttribute('data-look', 'paper');
  } catch (e) { /* never block the page */ }
})();
