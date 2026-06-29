// Reusable client-side table filtering + click-to-sort. Wire it up with markup:
//
//   <div class="tablebar" data-table-filter="#apps-table">
//     <input type="search" data-tf="search">
//     <select data-tf="col" data-tf-col="3"><option value="">all</option></select>
//     <input type="number" data-tf="minmatch" data-tf-col="2">
//     <span data-tf="count"></span>
//   </div>
//   <table id="apps-table">…</table>
//
// Column <select>s are auto-populated with the distinct values in that column.
// Filtering is instant and case-insensitive; non-matching rows are hidden and a
// "Showing X of Y" count is updated. Every header is also click-to-sort
// (numeric / text auto-detected; click again to reverse). Tables referenced by
// a filter bar get sorting automatically; any other table can opt in with the
// data-sortable attribute.

(function () {
  // Prefer a status/fit pill's label over raw cell text, so decorations like
  // the "⏰" saved-icon don't pollute dropdown values or sort keys.
  function colValue(cell) {
    if (!cell) return '';
    var tag = cell.querySelector('.pill, .fit');
    // A data-val attribute (e.g. the AI tier YES/MAYBE/NO behind a "85%" badge)
    // wins, so filtering/sorting use the tier rather than the displayed number.
    if (tag && tag.hasAttribute('data-val')) return tag.getAttribute('data-val').trim();
    return (tag ? tag.textContent : cell.textContent).trim();
  }

  function bodyRows(table) {
    if (!table.tBodies.length) return [];
    return Array.prototype.slice.call(table.tBodies[0].rows).filter(function (r) {
      return !r.querySelector('td[colspan]');   // skip the "nothing here" row
    });
  }

  // ---- Filtering ----
  function initBar(bar) {
    var table = document.querySelector(bar.getAttribute('data-table-filter'));
    if (!table || !table.tBodies.length) return;
    var rows = bodyRows(table);
    var search = bar.querySelector('[data-tf="search"]');
    var selects = Array.prototype.slice.call(bar.querySelectorAll('[data-tf="col"]'));
    var minMatch = bar.querySelector('[data-tf="minmatch"]');
    var countEl = bar.querySelector('[data-tf="count"]');

    selects.forEach(function (sel) {
      var ci = parseInt(sel.getAttribute('data-tf-col'), 10);
      var seen = {};
      rows.forEach(function (r) {
        var t = colValue(r.cells[ci]);
        if (t) seen[t] = true;
      });
      Object.keys(seen).sort().forEach(function (v) {
        var o = document.createElement('option');
        o.value = v.toLowerCase(); o.textContent = v;
        sel.appendChild(o);
      });
    });

    function apply() {
      var q = (search && search.value || '').trim().toLowerCase();
      var min = minMatch ? parseFloat(minMatch.value) : NaN;
      var minCol = minMatch ? parseInt(minMatch.getAttribute('data-tf-col'), 10) : -1;
      var shown = 0;
      rows.forEach(function (r) {
        var ok = true;
        if (q && r.textContent.toLowerCase().indexOf(q) === -1) ok = false;
        if (ok) {
          for (var i = 0; i < selects.length; i++) {
            var sel = selects[i];
            if (!sel.value) continue;
            var t = colValue(r.cells[parseInt(sel.getAttribute('data-tf-col'), 10)]).toLowerCase();
            if (t !== sel.value) { ok = false; break; }
          }
        }
        if (ok && minMatch && !isNaN(min)) {
          var mc = r.cells[minCol];
          var pct = parseFloat((mc ? mc.textContent : '').replace('%', '')) || 0;
          if (pct < min) ok = false;
        }
        r.style.display = ok ? '' : 'none';
        if (ok) shown++;
      });
      if (countEl) countEl.textContent = 'Showing ' + shown + ' of ' + rows.length;
    }

    if (search) search.addEventListener('input', apply);
    selects.forEach(function (s) { s.addEventListener('change', apply); });
    if (minMatch) minMatch.addEventListener('input', apply);
    apply();
  }

  // ---- Click-to-sort ----
  function makeSortable(table) {
    if (!table || table.__sortable || !table.tHead || !table.tBodies.length) return;
    table.__sortable = true;
    var tbody = table.tBodies[0];
    var headers = Array.prototype.slice.call(table.tHead.rows[0].cells);

    function isNumericCol(rows, idx) {
      var any = false;
      for (var i = 0; i < rows.length; i++) {
        var t = colValue(rows[i].cells[idx]).replace('%', '').trim();
        if (t === '') continue;
        any = true;
        if (!/^-?\d+(\.\d+)?$/.test(t)) return false;
      }
      return any;
    }

    function sortBy(idx, asc) {
      var rows = bodyRows(table);
      var numeric = isNumericCol(rows, idx);
      rows.sort(function (a, b) {
        var ta = colValue(a.cells[idx]), tb = colValue(b.cells[idx]);
        var cmp;
        if (numeric) {
          var na = parseFloat(ta.replace('%', '')); if (isNaN(na)) na = -Infinity;
          var nb = parseFloat(tb.replace('%', '')); if (isNaN(nb)) nb = -Infinity;
          cmp = na - nb;
        } else {
          // empty values always sort to the bottom regardless of direction
          if (!ta && tb) return 1;
          if (ta && !tb) return -1;
          cmp = ta.toLowerCase().localeCompare(tb.toLowerCase());
        }
        return asc ? cmp : -cmp;
      });
      rows.forEach(function (r) { tbody.appendChild(r); });
    }

    headers.forEach(function (th, idx) {
      th.classList.add('sortable');
      th.addEventListener('click', function () {
        var asc = th.getAttribute('data-sort') !== 'asc';
        headers.forEach(function (h) {
          h.classList.remove('sorted-asc', 'sorted-desc');
          if (h !== th) h.removeAttribute('data-sort');
        });
        th.setAttribute('data-sort', asc ? 'asc' : 'desc');
        th.classList.add(asc ? 'sorted-asc' : 'sorted-desc');
        sortBy(idx, asc);
      });
    });
  }

  document.querySelectorAll('[data-table-filter]').forEach(function (bar) {
    initBar(bar);
    makeSortable(document.querySelector(bar.getAttribute('data-table-filter')));
  });
  document.querySelectorAll('table[data-sortable]').forEach(makeSortable);
})();
