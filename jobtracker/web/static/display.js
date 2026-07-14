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

  function theme() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
  }

  function fontIdx() {
    var f = document.documentElement.getAttribute('data-font') || 'md';
    var i = FONT_LEVELS.indexOf(f);
    return i >= 0 ? i : 1;
  }

  function applyTheme(next) {
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    var btn = document.getElementById('theme-toggle');
    if (btn) {
      btn.textContent = next === 'light' ? '☀️' : '🌙';
      btn.title = next === 'light' ? 'Switch to dark mode' : 'Switch to light mode';
      btn.setAttribute('aria-label', btn.title);
    }
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

    var toggle = document.getElementById('theme-toggle');
    if (toggle) {
      toggle.addEventListener('click', function () {
        applyTheme(theme() === 'light' ? 'dark' : 'light');
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
