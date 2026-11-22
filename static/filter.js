(function () {
  const table = document.getElementById('k-bugs-table');
  if (!table) return;

  const anchor = new Date(table.getAttribute('data-as-of'));
  const bugRows = Array.from(table.querySelectorAll('tbody tr.k-bug'));
  const noMatchesRow = document.getElementById('k-no-matches');
  const pkgInput = document.getElementById('k-pkg-search');
  const clearBtn = document.getElementById('k-filter-clear');
  const countSpan = document.getElementById('k-filter-count');
  const verboseToggle = document.getElementById('k-verbose-toggle');
  const rangeContainer = document.getElementById('k-ranges');
  const rangeBtns = rangeContainer ? Array.from(rangeContainer.querySelectorAll('.k-range-btn')) : [];
  const dateFrom = document.getElementById('k-date-from');
  const dateTo = document.getElementById('k-date-to');

  const defaultSlug = '24-hours';
  const total = bugRows.length;
  let currentView = 'brief';
  let rangeMode = 'preset';
  let rangeSlug = defaultSlug;
  let rangeDays = 1;

  // Multi-select pill group with a leading "all" pill. get() returns null
  // when everything passes, or the set of selected values (lowercase).
  function makePillGroup(containerId, attr) {
    const container = document.getElementById(containerId);
    const allPill = container ? container.querySelector('.k-pill[data-' + attr + '="all"]') : null;
    const pills = container ? Array.from(container.querySelectorAll('.k-pill:not([data-' + attr + '="all"])')) : [];

    function get() {
      if (!allPill || allPill.classList.contains('is-active')) {
        return null;
      }
      const active = new Set();
      pills.forEach(p => {
        if (p.classList.contains('is-active')) {
          active.add(p.getAttribute('data-' + attr).toLowerCase());
        }
      });
      return active.size > 0 ? active : null;
    }

    function set(values) {
      if (!container) return;
      const targets = (values || []).map(v => v.toLowerCase());
      let any = false;
      pills.forEach(p => {
        const on = targets.includes(p.getAttribute('data-' + attr).toLowerCase());
        p.classList.toggle('is-active', on);
        if (on) any = true;
      });
      if (allPill) {
        allPill.classList.toggle('is-active', !any);
      }
    }

    if (container) {
      container.addEventListener('click', e => {
        const pill = e.target.closest('.k-pill');
        if (!pill) return;
        if (pill.getAttribute('data-' + attr) === 'all') {
          if (allPill) allPill.classList.add('is-active');
          pills.forEach(p => p.classList.remove('is-active'));
        } else {
          pill.classList.toggle('is-active');
          const any = pills.some(p => p.classList.contains('is-active'));
          if (allPill) allPill.classList.toggle('is-active', !any);
        }
        applyFilters();
      });
    }

    return { get, set };
  }

  const seriesPills = makePillGroup('k-series-pills', 'series');
  const statePills = makePillGroup('k-state-pills', 'state');

  // Pair each bug row with its verbose detail rows (if any). Clicking
  // anywhere on a row (except links) toggles its details.
  const detailOf = new Map();
  const openedOf = new Map();
  bugRows.forEach(row => {
    const details = [];
    let next = row.nextElementSibling;
    while (next && next.classList.contains('k-details')) {
      details.push(next);
      next = next.nextElementSibling;
    }
    if (details.length) {
      detailOf.set(row, details);
    }
    openedOf.set(row, new Date(row.getAttribute('data-opened')));
    row.addEventListener('click', e => {
      if (e.target.closest('a')) return;
      if (!detailOf.has(row)) return;
      row.classList.toggle('is-open');
      syncDetails(row);
    });
  });

  // A detail row is shown iff its bug row passes the filters, the bug is
  // expanded (verbose view or individually opened), and the task's own
  // state matches the state filter.
  function syncDetails(row) {
    const details = detailOf.get(row);
    if (!details) return;
    const expanded = row.style.display !== 'none' && (currentView === 'verbose' || row.classList.contains('is-open'));
    const states = statePills.get();
    details.forEach(d => {
      const show = expanded && (states === null || states.has(d.getAttribute('data-state')));
      d.style.display = show ? 'table-row' : 'none';
    });
  }

  function baseMatch(row) {
    const pkgQuery = pkgInput ? pkgInput.value.trim().toLowerCase() : '';
    const activeSeries = seriesPills.get();
    const activeStates = statePills.get();

    if (activeStates !== null && !activeStates.has(row.getAttribute('data-state'))) {
      return false;
    }

    if (activeSeries !== null) {
      const rowSeries = (row.getAttribute('data-series') || '').toLowerCase().split(/\s+/).filter(s => s);
      const none = rowSeries.length === 0 && activeSeries.has('none');
      const hit = rowSeries.some(s => activeSeries.has(s));
      if (!none && !hit) return false;
    }

    if (pkgQuery && (row.getAttribute('data-targets') || '').toLowerCase().indexOf(pkgQuery) === -1) {
      return false;
    }

    return true;
  }

  function presetMatch(row, days) {
    const cutoff = new Date(anchor.getTime() - days * 86400000);
    return openedOf.get(row) >= cutoff;
  }

  function customMatch(row) {
    const opened = openedOf.get(row);
    if (dateFrom && dateFrom.value && opened < new Date(dateFrom.value + 'T00:00:00Z')) {
      return false;
    }
    if (dateTo && dateTo.value && opened > new Date(dateTo.value + 'T23:59:59.999Z')) {
      return false;
    }
    return true;
  }

  function dateMatch(row) {
    return rangeMode === 'preset' ? presetMatch(row, rangeDays) : customMatch(row);
  }

  function setRowVisible(row, visible) {
    row.style.display = visible ? '' : 'none';
    syncDetails(row);
  }

  function applyFilters() {
    let visible = 0;
    bugRows.forEach(row => {
      const show = baseMatch(row) && dateMatch(row);
      setRowVisible(row, show);
      if (show) visible++;
    });

    if (noMatchesRow) {
      noMatchesRow.style.display = visible === 0 ? '' : 'none';
    }

    if (countSpan) {
      countSpan.textContent = `Showing ${visible} of ${total} bug${total === 1 ? '' : 's'}`;
    }

    // Live per-preset counts, reflecting every non-date filter.
    rangeBtns.forEach(btn => {
      const days = parseInt(btn.getAttribute('data-days'), 10);
      let n = 0;
      bugRows.forEach(row => {
        if (baseMatch(row) && presetMatch(row, days)) n++;
      });
      const badge = btn.querySelector('.k-range-count');
      if (badge) badge.textContent = n;
    });

    updateURL();
  }

  function updateURL() {
    if (!window.history || !window.history.replaceState) return;

    const params = new URLSearchParams(window.location.search);
    const pkgQuery = pkgInput ? pkgInput.value.trim().toLowerCase() : '';
    if (pkgQuery) {
      params.set('pkg', pkgQuery);
    } else {
      params.delete('pkg');
    }

    const activeSeries = seriesPills.get();
    if (activeSeries !== null && activeSeries.size > 0) {
      params.set('series', Array.from(activeSeries).join(','));
    } else {
      params.delete('series');
    }

    const activeStates = statePills.get();
    if (activeStates !== null && activeStates.size > 0) {
      params.set('state', Array.from(activeStates).join(','));
    } else {
      params.delete('state');
    }
    params.delete('active');

    if (rangeMode === 'preset') {
      if (rangeSlug !== defaultSlug) {
        params.set('range', rangeSlug);
      } else {
        params.delete('range');
      }
      params.delete('from');
      params.delete('to');
    } else {
      params.delete('range');
      if (dateFrom && dateFrom.value) {
        params.set('from', dateFrom.value);
      } else {
        params.delete('from');
      }
      if (dateTo && dateTo.value) {
        params.set('to', dateTo.value);
      } else {
        params.delete('to');
      }
    }

    if (currentView !== 'brief') {
      params.set('view', currentView);
    } else {
      params.delete('view');
    }

    const qs = params.toString();
    const newUrl = window.location.pathname + (qs ? '?' + qs : '') + window.location.hash;
    window.history.replaceState(null, '', newUrl);
  }

  function setVerbose(verbose) {
    currentView = verbose ? 'verbose' : 'brief';
    if (verboseToggle) {
      verboseToggle.checked = verbose;
    }
    table.classList.toggle('k-verbose', verbose);
    bugRows.forEach(syncDetails);
    try {
      window.localStorage.setItem('k-view', currentView);
    } catch (e) { /* storage unavailable */ }
  }

  function setPreset(slug) {
    const btn = rangeBtns.find(b => b.getAttribute('data-slug') === slug);
    rangeMode = 'preset';
    rangeSlug = btn ? slug : defaultSlug;
    rangeDays = btn ? parseInt(btn.getAttribute('data-days'), 10) : 1;
    rangeBtns.forEach(b => b.classList.toggle('is-active', b === btn));
    if (dateFrom) dateFrom.value = '';
    if (dateTo) dateTo.value = '';
  }

  function setCustom() {
    rangeMode = 'custom';
    rangeBtns.forEach(b => b.classList.remove('is-active'));
  }

  function initFromURL() {
    const params = new URLSearchParams(window.location.search);

    const urlPkg = params.get('pkg');
    if (urlPkg && pkgInput) {
      pkgInput.value = urlPkg;
    }

    const urlSeries = params.get('series');
    if (urlSeries) {
      seriesPills.set(urlSeries.split(','));
    }

    const urlState = params.get('state');
    if (urlState) {
      statePills.set(urlState.split(','));
    }

    const from = params.get('from');
    const to = params.get('to');
    const range = params.get('range');
    if (from || to) {
      if (dateFrom) dateFrom.value = from || '';
      if (dateTo) dateTo.value = to || '';
      setCustom();
    } else {
      setPreset(range || defaultSlug);
    }

    let initialView = params.get('view');
    if (!initialView) {
      try {
        initialView = window.localStorage.getItem('k-view');
      } catch (e) { /* storage unavailable */ }
    }
    setVerbose(initialView === 'verbose');
  }

  if (rangeContainer) {
    rangeContainer.addEventListener('click', e => {
      const btn = e.target.closest('.k-range-btn');
      if (!btn) return;
      setPreset(btn.getAttribute('data-slug'));
      applyFilters();
    });
  }

  [dateFrom, dateTo].forEach(input => {
    if (input) {
      input.addEventListener('change', () => {
        setCustom();
        applyFilters();
      });
    }
  });

  if (verboseToggle) {
    verboseToggle.addEventListener('change', () => {
      setVerbose(verboseToggle.checked);
      applyFilters();
    });
  }

  if (pkgInput) {
    pkgInput.addEventListener('input', applyFilters);
  }

  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      if (pkgInput) pkgInput.value = '';
      applyFilters();
    });
  }

  initFromURL();
  applyFilters();
})();
