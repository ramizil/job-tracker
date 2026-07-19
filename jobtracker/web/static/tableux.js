// Sticky header + dense rows + column show/hide for big data tables.
// Wire with: <div class="tablebar" data-table-ux="#apps-table">…</div>
//            <table id="apps-table" class="grid sticky-head">…
(function () {
  var DENSE_KEY = 'jt-table-dense';

  function applyDense(on) {
    document.documentElement.classList.toggle('table-dense', !!on);
    try { localStorage.setItem(DENSE_KEY, on ? '1' : '0'); } catch (e) {}
    document.querySelectorAll('[data-tf-dense]').forEach(function (btn) {
      btn.classList.toggle('on', !!on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.title = on ? 'Comfortable row height' : 'Denser rows (more on screen)';
    });
  }

  function colKey(tableId) {
    return 'jt-cols:' + tableId;
  }

  function loadHidden(tableId) {
    try {
      var raw = localStorage.getItem(colKey(tableId));
      var arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch (e) {
      return [];
    }
  }

  function saveHidden(tableId, idxs) {
    try { localStorage.setItem(colKey(tableId), JSON.stringify(idxs)); } catch (e) {}
  }

  function setColVisible(table, colIdx, visible) {
    var sel = 'th:nth-child(' + (colIdx + 1) + '), td:nth-child(' + (colIdx + 1) + ')';
    table.querySelectorAll(sel).forEach(function (cell) {
      cell.classList.toggle('col-hidden', !visible);
    });
  }

  function initTable(bar) {
    var sel = bar.getAttribute('data-table-ux');
    if (!sel) return;
    var table = document.querySelector(sel);
    if (!table) return;
    table.classList.add('sticky-head');
    var tableId = table.id || sel.replace('#', '');

    // Dense toggle (shared globally)
    if (!bar.querySelector('[data-tf-dense]')) {
      var denseBtn = document.createElement('button');
      denseBtn.type = 'button';
      denseBtn.className = 'btn sm';
      denseBtn.setAttribute('data-tf-dense', '1');
      denseBtn.textContent = 'Dense';
      bar.appendChild(denseBtn);
      denseBtn.addEventListener('click', function () {
        applyDense(!document.documentElement.classList.contains('table-dense'));
      });
    }

    // Columns menu
    var heads = table.tHead ? Array.prototype.slice.call(table.tHead.rows[0].cells) : [];
    if (!heads.length || bar.querySelector('[data-tf-cols]')) return;

    var wrap = document.createElement('details');
    wrap.className = 'cols-menu';
    wrap.setAttribute('data-tf-cols', '1');
    var sum = document.createElement('summary');
    sum.className = 'btn sm';
    sum.textContent = 'Columns';
    wrap.appendChild(sum);
    var panel = document.createElement('div');
    panel.className = 'cols-panel';

    var hidden = loadHidden(tableId);
    heads.forEach(function (th, i) {
      // Never hide checkbox / mail / actions-ish first col if empty header
      var label = (th.textContent || '').trim() || ('Col ' + (i + 1));
      var id = tableId + '-col-' + i;
      var lab = document.createElement('label');
      lab.className = 'cols-item';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.id = id;
      cb.checked = hidden.indexOf(i) < 0;
      cb.addEventListener('change', function () {
        var next = loadHidden(tableId).filter(function (x) { return x !== i; });
        if (!cb.checked) next.push(i);
        next.sort(function (a, b) { return a - b; });
        saveHidden(tableId, next);
        setColVisible(table, i, cb.checked);
      });
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(' ' + label));
      panel.appendChild(lab);
      if (hidden.indexOf(i) >= 0) setColVisible(table, i, false);
    });
    wrap.appendChild(panel);
    bar.appendChild(wrap);
  }

  function init() {
    var dense = false;
    try { dense = localStorage.getItem(DENSE_KEY) === '1'; } catch (e) {}
    applyDense(dense);
    document.querySelectorAll('[data-table-ux]').forEach(initTable);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
