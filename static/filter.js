(function () {
  const table = document.getElementById('k-bugs-table');
  if (!table) return;

  const rows = Array.from(table.querySelectorAll('tbody tr:not(#k-no-matches)'));
  const noMatchesRow = document.getElementById('k-no-matches');
  const pkgInput = document.getElementById('k-pkg-search');
  const clearBtn = document.getElementById('k-filter-clear');
  const countSpan = document.getElementById('k-filter-count');
  const pillsContainer = document.getElementById('k-series-pills');
  const allPill = pillsContainer ? pillsContainer.querySelector('[data-series="all"]') : null;
  const seriesPills = pillsContainer ? Array.from(pillsContainer.querySelectorAll('.k-pill:not([data-series="all"])')) : [];

  const total = rows.length;

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

  function applyFilters() {
    const pkgQuery = pkgInput ? pkgInput.value.trim().toLowerCase() : '';
    const activeSeries = getActiveSeries();

    let visible = 0;

    rows.forEach(row => {
      const rowSeries = (row.getAttribute('data-series') || '').toLowerCase();
      const rowTargets = (row.getAttribute('data-targets') || '').toLowerCase();

      let seriesMatch = false;
      if (activeSeries === null) {
        seriesMatch = true;
      } else if (rowSeries === '' && activeSeries.has('none')) {
        seriesMatch = true;
      } else if (rowSeries !== '' && activeSeries.has(rowSeries)) {
        seriesMatch = true;
      }

      let pkgMatch = false;
      if (!pkgQuery || rowTargets.indexOf(pkgQuery) !== -1) {
        pkgMatch = true;
      }

      if (seriesMatch && pkgMatch) {
        row.style.display = '';
        visible++;
      } else {
        row.style.display = 'none';
      }
    });

    if (noMatchesRow) {
      noMatchesRow.style.display = visible === 0 ? '' : 'none';
    }

    if (countSpan) {
      countSpan.textContent = `Showing ${visible} of ${total} bug${total === 1 ? '' : 's'}`;
    }

    updateURL(pkgQuery, activeSeries);
  }

  function updateURL(pkgQuery, activeSeries) {
    if (!window.history || !window.history.replaceState) return;

    const params = new URLSearchParams(window.location.search);
    if (pkgQuery) {
      params.set('pkg', pkgQuery);
    } else {
      params.delete('pkg');
    }

    if (activeSeries !== null && activeSeries.size > 0) {
      params.set('series', Array.from(activeSeries).join(','));
    } else {
      params.delete('series');
    }

    const qs = params.toString();
    const newUrl = window.location.pathname + (qs ? '?' + qs : '') + window.location.hash;
    window.history.replaceState(null, '', newUrl);
  }

  function initFromURL() {
    const params = new URLSearchParams(window.location.search);
    const urlPkg = params.get('pkg');
    const urlSeries = params.get('series');

    if (urlPkg && pkgInput) {
      pkgInput.value = urlPkg;
    }

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

  if (pkgInput) {
    pkgInput.addEventListener('input', applyFilters);
  }

  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      if (pkgInput) pkgInput.value = '';
      if (allPill) allPill.classList.add('is-active');
      seriesPills.forEach(p => p.classList.remove('is-active'));
      applyFilters();
    });
  }

  initFromURL();
  applyFilters();
})();
