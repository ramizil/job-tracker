// Reusable client-side table filtering. Wire it up with markup like:
//
//   <div class="tablebar" data-table-filter="#apps-table">
//     <input type="search" data-tf="search">
//     <select data-tf="col" data-tf-col="3"><option value="">all</option></select>
//     <input type="number" data-tf="minmatch" data-tf-col="2">
//     <span data-tf="count"></span>
//   </div>
//   <table id="apps-table">…</table>
//
// Column <select>s are auto-populated with the distinct values found in that
// column. Filtering is instant and case-insensitive; rows that don't match are
// hidden and a "Showing X of Y" count is updated.
(function () {
  function initBar(bar) {
    var table = document.querySelector(bar.getAttribute('data-table-filter'));
    if (!table || !table.tBodies.length) return;
    var rows = Array.prototype.slice.call(table.tBodies[0].rows).filter(function (r) {
      return !r.querySelector('td[colspan]');   // skip the "nothing here" row
    });
    var search = bar.querySelector('[data-tf="search"]');
    var selects = Array.prototype.slice.call(bar.querySelectorAll('[data-tf="col"]'));
    var minMatch = bar.querySelector('[data-tf="minmatch"]');
    var countEl = bar.querySelector('[data-tf="count"]');

    // Prefer a status/fit pill's label over the raw cell text, so decorations
    // like the "⏰" saved-icon don't pollute the dropdown values.
    function colValue(cell) {
      if (!cell) return '';
      var tag = cell.querySelector('.pill, .fit');
      return (tag ? tag.textContent : cell.textContent).trim();
    }

    // Populate each column <select> with the distinct values in that column.
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
            var ci = parseInt(sel.getAttribute('data-tf-col'), 10);
            var t = colValue(r.cells[ci]).toLowerCase();
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

  document.querySelectorAll('[data-table-filter]').forEach(initBar);
})();
