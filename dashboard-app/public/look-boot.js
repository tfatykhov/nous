// The dashboard's look, set on <html> before the app loads so the page never flashes the other look. The same rule
// as resolveLook() in src/lib/look.ts (look.test.ts runs both on one table): ?look=paper|dark wins and is
// remembered, else the remembered look, else dark (no attribute). Storage can throw (private mode): the URL still
// applies. An applied ?look= is then removed from the URL (the hash and other parameters stay), so a bookmark or a
// reload never re-applies it over the switch.
(function () {
  try {
    var key = 'nous.look';
    var look = null;
    var params = new URLSearchParams(location.search);
    var fromUrl = params.get('look');
    if (fromUrl === 'paper' || fromUrl === 'dark') {
      look = fromUrl;
      try { localStorage.setItem(key, look); } catch (e) { /* not remembered */ }
      try {
        params.delete('look');
        var rest = params.toString();
        history.replaceState(history.state, '', location.pathname + (rest ? '?' + rest : '') + location.hash);
      } catch (e) { /* the parameter stays */ }
    } else {
      try {
        var stored = localStorage.getItem(key);
        if (stored === 'paper' || stored === 'dark') look = stored;
      } catch (e) { /* no memory: dark */ }
    }
    if (look === 'paper') document.documentElement.setAttribute('data-look', 'paper');
  } catch (e) { /* never block the page */ }
})();
