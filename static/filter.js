(function () {
  const table = document.getElementById('k-bugs-table');
  if (!table) return;

  const anchor = new Date(table.getAttribute('data-generated'));
  const bugRows = Array.from(table.querySelectorAll('tbody tr.k-bug'));
  const noMatchesRow = document.getElementById('k-no-matches');
  const pkgInput = document.getElementById('k-pkg-search');
  const clearBtn = document.getElementById('k-filter-clear');
  const countSpan = document.getElementById('k-filter-count');
  const pillsContainer = document.getElementById('k-series-pills');
  const allPill = pillsContainer ? pillsContainer.querySelector('[data-series="all"]') : null;
  const seriesPills = pillsContainer ? Array.from(pillsContainer.querySelectorAll('.k-pill:not([data-series="all"])')) : [];
  const verboseToggle = document.getElementById('k-verbose-toggle');
  const activeChk = document.getElementById('k-active-only');
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
  let activeOnly = false;

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

  // A detail row is shown iff its bug row passes the filters and either
  // verbose view is on or the bug is individually expanded.
  function syncDetails(row) {
    const details = detailOf.get(row);
    if (!details) return;
    const show = row.style.display !== 'none' && (currentView === 'verbose' || row.classList.contains('is-open'));
    details.forEach(d => {
      d.style.display = show ? 'table-row' : 'none';
    });
  }

  function getActiveSeries() {
    if (!allPill || allPill.classList.contains('is-active')) {
      return null;
    }
    const active = new Set();
    seriesPills.forEach(pill => {
      if (pill.classList.contains('is-active')) {
        active.add(pill.getAttribute('data-series').toLowerCase());
      }
    });
    return active.size > 0 ? active : null;
  }

  function baseMatch(row) {
    const pkgQuery = pkgInput ? pkgInput.value.trim().toLowerCase() : '';
    const activeSeries = getActiveSeries();
    const rowSeries = (row.getAttribute('data-series') || '').toLowerCase().split(/\s+/).filter(s => s);
    const rowTargets = (row.getAttribute('data-targets') || '').toLowerCase();

    if (activeOnly && row.getAttribute('data-active') !== 'true') {
      return false;
    }

    if (activeSeries !== null) {
      const none = rowSeries.length === 0 && activeSeries.has('none');
      const hit = rowSeries.some(s => activeSeries.has(s));
      if (!none && !hit) return false;
    }

    if (pkgQuery && rowTargets.indexOf(pkgQuery) === -1) {
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

    const activeSeries = getActiveSeries();
    if (activeSeries !== null && activeSeries.size > 0) {
      params.set('series', Array.from(activeSeries).join(','));
    } else {
      params.delete('series');
    }

    if (activeOnly) {
      params.set('active', '1');
    } else {
      params.delete('active');
    }

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
    if (urlSeries && pillsContainer) {
      const targetSeries = urlSeries.toLowerCase().split(',').map(s => s.trim());
      let activatedAny = false;
      seriesPills.forEach(pill => {
        const s = pill.getAttribute('data-series').toLowerCase();
        if (targetSeries.includes(s)) {
          pill.classList.add('is-active');
          activatedAny = true;
        } else {
          pill.classList.remove('is-active');
        }
      });
      if (activatedAny && allPill) {
        allPill.classList.remove('is-active');
      }
    }

    activeOnly = params.get('active') === '1';
    if (activeChk) {
      activeChk.checked = activeOnly;
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

  if (pillsContainer) {
    pillsContainer.addEventListener('click', e => {
      const pill = e.target.closest('.k-pill');
      if (!pill) return;

      const seriesVal = pill.getAttribute('data-series');
      if (seriesVal === 'all') {
        allPill.classList.add('is-active');
        seriesPills.forEach(p => p.classList.remove('is-active'));
      } else {
        pill.classList.toggle('is-active');
        const anyActive = seriesPills.some(p => p.classList.contains('is-active'));
        if (anyActive) {
          if (allPill) allPill.classList.remove('is-active');
        } else {
          if (allPill) allPill.classList.add('is-active');
        }
      }
      applyFilters();
    });
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

  if (activeChk) {
    activeChk.addEventListener('change', () => {
      activeOnly = activeChk.checked;
      applyFilters();
    });
  }

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
