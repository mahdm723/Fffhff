// V6 phase 8: apply the saved appearance before the first paint (loaded in <head>; an external file because of
// the CSP). The account's choice replaces this local copy once /api/me arrives (js/appearance.js).
(function () {
  try {
    var a = JSON.parse(localStorage.getItem('dz.appearance') || 'null');
    if (!a) return;
    var r = document.documentElement;
    if (a.mode === 'light' || a.mode === 'dark') r.dataset.theme = a.mode;
    if (/^[a-z]{2,16}$/.test(a.accent || '')) r.dataset.accent = a.accent;
    if (a.font === 'large') r.dataset.font = 'large';
    var app = window.DZPLAYAndroid; // Android 3.0.0+: the system bar icons follow the chosen theme
    if (app && app.setTheme) app.setTheme(r.dataset.theme || 'system');
  } catch (e) { /* storage blocked or an older app: the defaults apply */ }
})();
