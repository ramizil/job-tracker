// Theme + font-size preferences (stored in localStorage, applied on <html>).
(function () {
  var THEME_KEY = 'jt-theme';
  var FONT_KEY = 'jt-font';
  var FONT_LEVELS = ['sm', 'md', 'lg', 'xl'];
  var FONT_LABELS = {
    sm: 'Small',
    md: 'Normal',
    lg: 'Large',
    xl: 'XL'
  };
  // Named skins. Legacy light/dark map to futuristic variants.
  var THEMES = {
    futuristic: 'Futuristic',
    'futuristic-light': 'Futuristic Light',
    professional: 'Professional',
    'professional-dark': 'Professional Dark',
    slate: 'Slate'
  };
  var LEGACY = { dark: 'futuristic', light: 'futuristic-light' };

  function normalizeTheme(raw) {
    var t = LEGACY[raw] || raw || 'futuristic';
    return THEMES[t] ? t : 'futuristic';
  }

  function theme() {
    return normalizeTheme(document.documentElement.getAttribute('data-theme'));
  }

  function fontIdx() {
    var f = document.documentElement.getAttribute('data-font') || 'md';
    var i = FONT_LEVELS.indexOf(f);
    return i >= 0 ? i : 1;
  }

  function applyTheme(next) {
    next = normalizeTheme(next);
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    var sel = document.getElementById('theme-select');
    if (sel && sel.value !== next) sel.value = next;
    // Notify fx.js (and anything else) that the skin changed.
    try {
      window.dispatchEvent(new CustomEvent('jt-theme', { detail: { theme: next } }));
    } catch (e) {}
  }

  function applyFont(level) {
    document.documentElement.setAttribute('data-font', level);
    try { localStorage.setItem(FONT_KEY, level); } catch (e) {}
    var label = document.getElementById('font-size-label');
    if (label) label.textContent = FONT_LABELS[level] || level;
    var down = document.getElementById('font-down');
    var up = document.getElementById('font-up');
    var i = FONT_LEVELS.indexOf(level);
    if (down) down.disabled = i <= 0;
    if (up) up.disabled = i >= FONT_LEVELS.length - 1;
  }

  function init() {
    if (!document.documentElement.getAttribute('data-font')) applyFont('md');
    applyTheme(theme());

    var sel = document.getElementById('theme-select');
    if (sel) {
      sel.addEventListener('change', function () {
        applyTheme(sel.value);
      });
    }
    // Keep a simple light/dark toggle as a shortcut cycling within the
    // active family when the legacy button is still present.
    var toggle = document.getElementById('theme-toggle');
    if (toggle) {
      toggle.addEventListener('click', function () {
        var t = theme();
        if (t === 'futuristic') applyTheme('futuristic-light');
        else if (t === 'futuristic-light') applyTheme('futuristic');
        else if (t === 'professional') applyTheme('professional-dark');
        else if (t === 'professional-dark') applyTheme('professional');
        else if (t === 'slate') applyTheme('professional-dark');
        else applyTheme('futuristic');
      });
    }
    var down = document.getElementById('font-down');
    if (down) {
      down.addEventListener('click', function () {
        var i = fontIdx();
        if (i > 0) applyFont(FONT_LEVELS[i - 1]);
      });
    }
    var up = document.getElementById('font-up');
    if (up) {
      up.addEventListener('click', function () {
        var i = fontIdx();
        if (i < FONT_LEVELS.length - 1) applyFont(FONT_LEVELS[i + 1]);
      });
    }
    applyFont(document.documentElement.getAttribute('data-font') || 'md');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
