/**
 * Turbo Drive navigation — no full tab refresh.
 * Loader stays alive (no unload freeze). Page boots on turbo:load.
 */
(function initTurboNav() {
    if (!window.Turbo) return;

    const MAX_MS = 8000;
    let safetyTimer = null;
    let visitPending = false;
    let submitPending = false;

    function clearSafety() {
        if (safetyTimer) {
            clearTimeout(safetyTimer);
            safetyTimer = null;
        }
    }

    function showNavLoader(msg) {
        document.documentElement.classList.remove('of-leaving');
        document.documentElement.classList.add('of-loading');
        if (window.ofLoader && typeof window.ofLoader.show === 'function') {
            window.ofLoader.show(msg || 'Loading…');
        } else {
            const node = document.getElementById('ofLoader');
            if (node) {
                node.classList.add('is-on');
                node.setAttribute('aria-hidden', 'false');
            }
            const label = document.getElementById('ofLoaderLabel');
            if (label && msg) label.textContent = msg;
        }
        clearSafety();
        safetyTimer = setTimeout(() => hideNavLoader(true), MAX_MS);
    }

    function hideNavLoader(force) {
        clearSafety();
        visitPending = false;
        submitPending = false;
        document.documentElement.classList.remove('of-loading', 'of-leaving');
        if (window.ofLoader && typeof window.ofLoader.hide === 'function') {
            window.ofLoader.hide(!!force);
        } else {
            const node = document.getElementById('ofLoader');
            if (node) {
                node.classList.remove('is-on');
                node.setAttribute('aria-hidden', 'true');
            }
        }
    }

    document.addEventListener('turbo:click', (event) => {
        const el = event.target.closest('a');
        if (!el) return;
        if (el.hasAttribute('data-turbo') && el.getAttribute('data-turbo') === 'false') return;
        if (el.target && el.target !== '_self') return;
        if (el.hasAttribute('download')) return;
    });

    document.addEventListener('turbo:before-visit', () => {
        visitPending = true;
        showNavLoader(submitPending ? 'Saving…' : 'Loading…');
    });

    document.addEventListener('turbo:before-fetch-request', (event) => {
        const headers = event.detail.fetchOptions.headers;
        headers.Accept = 'text/html, application/xhtml+xml';
        const method = String(event.detail.fetchOptions.method || 'GET').toUpperCase();
        if (method !== 'GET' && method !== 'HEAD') {
            submitPending = true;
            showNavLoader('Saving…');
            // Avoid double-count with main.js ofFetch wrapper
            event.detail.fetchOptions.ofSilent = true;
        }
    });

    document.addEventListener('turbo:load', () => {
        hideNavLoader(true);
        if (typeof window.ofBootUi === 'function') {
            window.ofBootUi();
        }
        if (typeof window.ofToastsRefresh === 'function') {
            window.ofToastsRefresh();
        }
        if (typeof window.ofTipsRefresh === 'function') {
            window.ofTipsRefresh(document);
        }
    });

    document.addEventListener('turbo:submit-start', () => {
        submitPending = true;
        showNavLoader('Saving…');
    });

    document.addEventListener('turbo:submit-end', (event) => {
        const ok = !!(event.detail && event.detail.success);
        // Successful saves usually navigate / re-render — keep loader until turbo:load
        if (ok || visitPending) return;
        hideNavLoader(true);
    });

    document.addEventListener('turbo:fetch-request-error', () => {
        hideNavLoader(true);
    });

    document.addEventListener('turbo:visit', () => {
        // close mobile sidebar if open
        document.body.classList.remove('sidebar-open');
        const backdrop = document.getElementById('sidebarBackdrop');
        if (backdrop) backdrop.hidden = true;
        document.body.style.overflow = '';
    });

    // Prefer Turbo for same-origin navigations; disable full-page showNav handoff
    window.__ofTurboNav = true;
})();
