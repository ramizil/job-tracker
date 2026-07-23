/* Switchable charts for Rejection insights (bars / pie / donut / columns). */
(function () {
  "use strict";

  var COLORS = [
    "#2563eb", "#dc2626", "#ca8a04", "#9333ea",
    "#0d9488", "#ea580c", "#16a34a", "#64748b",
  ];
  var STORAGE_KEY = "jt-chart-type";
  var TYPES = { bars: 1, pie: 1, donut: 1, cols: 1 };

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function labelOf(s) {
    return String(s || "").replace(/_/g, " ");
  }

  function parseItems(el) {
    try {
      var raw = JSON.parse(el.getAttribute("data-items") || "[]");
    } catch (e) {
      return [];
    }
    if (!Array.isArray(raw)) return [];
    return raw.map(function (row) {
      if (Array.isArray(row)) return { label: row[0], n: Number(row[1]) || 0 };
      return { label: row.label, n: Number(row.n || row.count) || 0 };
    }).filter(function (d) { return d.n > 0 || d.label; });
  }

  function totals(items) {
    var t = 0, m = 0;
    for (var i = 0; i < items.length; i++) {
      t += items[i].n;
      if (items[i].n > m) m = items[i].n;
    }
    return { total: t || 1, max: m || 1 };
  }

  function legendHtml(items, total) {
    var html = '<ul class="chart-legend">';
    for (var i = 0; i < items.length; i++) {
      var pct = Math.round((items[i].n / total) * 100);
      html += '<li><span class="chart-swatch" style="background:' +
        COLORS[i % COLORS.length] + '"></span>' +
        '<span class="chart-leg-label">' + esc(labelOf(items[i].label)) +
        '</span><span class="chart-leg-n">' + items[i].n +
        '</span><span class="chart-leg-pct">' + pct + "%</span></li>";
    }
    html += "</ul>";
    return html;
  }

  function renderBars(items) {
    var st = totals(items);
    if (!items.length) return '<p class="muted sm">No data yet.</p>';
    var html = '<div class="hbar-chart">';
    for (var i = 0; i < items.length; i++) {
      var pct = Math.round((items[i].n / st.total) * 100);
      var w = Math.floor((items[i].n / st.max) * 100);
      html += '<div class="hbar-row" title="' + esc(labelOf(items[i].label)) +
        ": " + items[i].n + " · " + pct + '% of total">' +
        '<span class="hbar-label">' + esc(labelOf(items[i].label)) + "</span>" +
        '<div class="hbar-track"><div class="hbar-fill" style="width:' + w +
        "%;background:" + COLORS[i % COLORS.length] + '"></div></div>' +
        '<span class="hbar-num">' + items[i].n + "</span>" +
        '<span class="hbar-pct">' + pct + "%</span></div>";
    }
    return html + "</div>";
  }

  function polar(cx, cy, r, angleDeg) {
    var rad = ((angleDeg - 90) * Math.PI) / 180;
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
  }

  function slicePath(cx, cy, r, start, end) {
    if (end - start >= 359.999) {
      // Full circle — two semicircles
      var m = polar(cx, cy, r, start + 180);
      var e = polar(cx, cy, r, start + 360);
      var s = polar(cx, cy, r, start);
      return "M " + s.x + " " + s.y +
        " A " + r + " " + r + " 0 1 1 " + m.x + " " + m.y +
        " A " + r + " " + r + " 0 1 1 " + e.x + " " + e.y + " Z";
    }
    var a0 = polar(cx, cy, r, start);
    var a1 = polar(cx, cy, r, end);
    var large = end - start > 180 ? 1 : 0;
    return "M " + cx + " " + cy +
      " L " + a0.x + " " + a0.y +
      " A " + r + " " + r + " 0 " + large + " 1 " + a1.x + " " + a1.y + " Z";
  }

  function renderPie(items, donut) {
    var st = totals(items);
    if (!items.length) return '<p class="muted sm">No data yet.</p>';
    var cx = 100, cy = 100, r = 88, ir = donut ? 48 : 0;
    var html = '<div class="pie-wrap"><svg class="pie-svg" viewBox="0 0 200 200" ' +
      'role="img" aria-label="' + (donut ? "Donut" : "Pie") + ' chart">';
    var angle = 0;
    for (var i = 0; i < items.length; i++) {
      var sweep = (items[i].n / st.total) * 360;
      var end = angle + sweep;
      var pct = Math.round((items[i].n / st.total) * 100);
      var title = esc(labelOf(items[i].label)) + ": " + items[i].n +
        " (" + pct + "%)";
      if (ir > 0) {
        // Donut: outer arc + reverse inner arc
        var o0 = polar(cx, cy, r, angle);
        var o1 = polar(cx, cy, r, end);
        var i1 = polar(cx, cy, ir, end);
        var i0 = polar(cx, cy, ir, angle);
        var large = sweep > 180 ? 1 : 0;
        var d;
        if (sweep >= 359.999) {
          d = "M " + (cx + r) + " " + cy +
            " A " + r + " " + r + " 0 1 1 " + (cx - r) + " " + cy +
            " A " + r + " " + r + " 0 1 1 " + (cx + r) + " " + cy +
            " M " + (cx + ir) + " " + cy +
            " A " + ir + " " + ir + " 0 1 0 " + (cx - ir) + " " + cy +
            " A " + ir + " " + ir + " 0 1 0 " + (cx + ir) + " " + cy + " Z";
        } else {
          d = "M " + o0.x + " " + o0.y +
            " A " + r + " " + r + " 0 " + large + " 1 " + o1.x + " " + o1.y +
            " L " + i1.x + " " + i1.y +
            " A " + ir + " " + ir + " 0 " + large + " 0 " + i0.x + " " + i0.y +
            " Z";
        }
        html += '<path class="pie-slice" d="' + d + '" fill="' +
          COLORS[i % COLORS.length] + '"><title>' + title + "</title></path>";
      } else {
        html += '<path class="pie-slice" d="' +
          slicePath(cx, cy, r, angle, end) + '" fill="' +
          COLORS[i % COLORS.length] + '"><title>' + title + "</title></path>";
      }
      angle = end;
    }
    if (donut) {
      html += '<text class="pie-center" x="100" y="96" text-anchor="middle">' +
        st.total + '</text>' +
        '<text class="pie-center-sub" x="100" y="114" text-anchor="middle">' +
        "total</text>";
    }
    html += "</svg>" + legendHtml(items, st.total) + "</div>";
    return html;
  }

  function renderCols(items) {
    var st = totals(items);
    if (!items.length) return '<p class="muted sm">No data yet.</p>';
    var html = '<div class="vbar-chart">';
    for (var i = 0; i < items.length; i++) {
      var pct = Math.round((items[i].n / st.total) * 100);
      var h = Math.max(2, Math.floor((items[i].n / st.max) * 100));
      html += '<div class="vbar-col" title="' + esc(labelOf(items[i].label)) +
        ": " + items[i].n + " · " + pct + '%">' +
        '<span class="vbar-num">' + items[i].n + "</span>" +
        '<div class="vbar-track"><div class="vbar-fill" style="height:' + h +
        "%;background:" + COLORS[i % COLORS.length] + '"></div></div>' +
        '<span class="vbar-label">' + esc(labelOf(items[i].label)) +
        "</span></div>";
    }
    return html + "</div>";
  }

  function render(el, type) {
    var items = parseItems(el);
    var canvas = el.querySelector(".chart-canvas");
    if (!canvas) return;
    if (type === "pie") canvas.innerHTML = renderPie(items, false);
    else if (type === "donut") canvas.innerHTML = renderPie(items, true);
    else if (type === "cols") canvas.innerHTML = renderCols(items);
    else canvas.innerHTML = renderBars(items);

    var buttons = el.querySelectorAll(".chart-type");
    for (var i = 0; i < buttons.length; i++) {
      var on = buttons[i].getAttribute("data-type") === type;
      buttons[i].classList.toggle("primary", on);
      buttons[i].setAttribute("aria-pressed", on ? "true" : "false");
    }
  }

  function currentType() {
    try {
      var t = localStorage.getItem(STORAGE_KEY) || "bars";
      return TYPES[t] ? t : "bars";
    } catch (e) {
      return "bars";
    }
  }

  function setType(type) {
    if (!TYPES[type]) type = "bars";
    try { localStorage.setItem(STORAGE_KEY, type); } catch (e) { /* ignore */ }
    var nodes = document.querySelectorAll("[data-sw-chart]");
    for (var i = 0; i < nodes.length; i++) render(nodes[i], type);
  }

  function init() {
    var nodes = document.querySelectorAll("[data-sw-chart]");
    if (!nodes.length) return;
    var type = currentType();
    for (var i = 0; i < nodes.length; i++) {
      (function (el) {
        el.addEventListener("click", function (ev) {
          var btn = ev.target.closest(".chart-type");
          if (!btn || !el.contains(btn)) return;
          setType(btn.getAttribute("data-type"));
        });
        render(el, type);
      })(nodes[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
