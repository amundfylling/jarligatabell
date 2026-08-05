/**
 * Jærligaen Static Site Enhancements
 */
document.addEventListener('DOMContentLoaded', () => {
  initViewSwitcher();
  initMobileRowDetails();
  initSeasonSelector();
  initScrollHint();
});

/**
 * Switch between 'Sammendrag' (Summary) and 'Alle ligaer' (Full Matrix) views
 */
function initViewSwitcher() {
  const btnSummary = document.getElementById('btn-view-summary');
  const btnFull = document.getElementById('btn-view-full');
  const panelSummary = document.getElementById('view-summary');
  const panelFull = document.getElementById('view-full');

  if (!btnSummary || !btnFull || !panelSummary || !panelFull) return;

  function setView(view) {
    const isSummary = view === 'summary';
    
    btnSummary.setAttribute('aria-selected', isSummary ? 'true' : 'false');
    btnFull.setAttribute('aria-selected', isSummary ? 'false' : 'true');

    if (isSummary) {
      panelSummary.removeAttribute('hidden');
      panelFull.setAttribute('hidden', '');
    } else {
      panelFull.removeAttribute('hidden');
      panelSummary.setAttribute('hidden', '');
    }

    try {
      sessionStorage.setItem('jarligaen_active_view', view);
    } catch (e) {
      // ignore storage errors
    }
  }

  btnSummary.addEventListener('click', () => setView('summary'));
  btnFull.addEventListener('click', () => setView('full'));

  // Restore saved preference if any
  try {
    const savedView = sessionStorage.getItem('jarligaen_active_view');
    if (savedView === 'full') {
      setView('full');
    }
  } catch (e) {}
}

/**
 * Accordion expand/collapse for mobile player details in Summary table
 */
function initMobileRowDetails() {
  const triggers = document.querySelectorAll('.btn-row-expand');
  
  triggers.forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const targetId = btn.getAttribute('aria-controls');
      if (!targetId) return;

      const detailsRow = document.getElementById(targetId);
      if (!detailsRow) return;

      const isExpanded = btn.getAttribute('aria-expanded') === 'true';
      btn.setAttribute('aria-expanded', isExpanded ? 'false' : 'true');
      
      if (isExpanded) {
        detailsRow.setAttribute('hidden', '');
      } else {
        detailsRow.removeAttribute('hidden');
      }
    });
  });
}

/**
 * Navigate to static season pages when select dropdown changes
 */
function initSeasonSelector() {
  const select = document.getElementById('season-select');
  if (!select) return;

  select.addEventListener('change', (e) => {
    const targetUrl = e.target.value;
    if (targetUrl) {
      window.location.href = targetUrl;
    }
  });
}

/**
 * Auto-hide horizontal scroll hint on interaction
 */
function initScrollHint() {
  const scrollContainer = document.querySelector('.table-scroll');
  const hint = document.getElementById('scroll-hint');
  
  if (!scrollContainer || !hint) return;

  function hideHint() {
    hint.style.display = 'none';
    scrollContainer.removeEventListener('scroll', hideHint);
  }

  scrollContainer.addEventListener('scroll', hideHint, { passive: true });
}
