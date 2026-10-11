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
// V6 phase 12: the language and the direction before the first paint (the same choice as js/i18n.js): the saved
// one, otherwise the phone's language (Arabic, French, otherwise English).
(function () {
  try {
    var langs = ['ar', 'fr', 'en'];
    var code = null;
    try { code = localStorage.getItem('dz.lang'); } catch (e) { /* storage blocked */ }
    if (langs.indexOf(code) < 0) {
      code = 'en';
      var list = navigator.languages || [navigator.language || ''];
      for (var i = 0; i < list.length; i++) {
        var c = String(list[i]).toLowerCase().split('-')[0];
        if (langs.indexOf(c) >= 0) { code = c; break; }
      }
    }
    document.documentElement.lang = code;
    document.documentElement.dir = code === 'ar' ? 'rtl' : 'ltr';
  } catch (e) { /* keep the page's own lang/dir */ }
})();

