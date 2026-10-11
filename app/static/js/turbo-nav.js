/**
 * Turbo Drive navigation — in-tab loader only (no full tab refresh).
 * Never show loader for prefetch hover requests (that was sticking the spinner).
 */
(function initTurboNav() {
    if (!window.Turbo) return;

    const MAX_MS = 45000;
    let safetyTimer = null;
    let visitPending = false;
    let submitPending = false;
    let loaderShown = false;

    function clearSafety() {
        if (safetyTimer) {
            clearTimeout(safetyTimer);
            safetyTimer = null;
        }
    }

    function isPrefetch(fetchOptions) {
        if (!fetchOptions) return false;
        const headers = fetchOptions.headers || {};
        const purpose = String(
            headers['X-Sec-Purpose']
            || headers['Sec-Purpose']
            || headers['Purpose']
            || ''
        ).toLowerCase();
        if (purpose.includes('prefetch')) return true;
        // Turbo may mark prefetch on the options object
        if (fetchOptions.headers && fetchOptions.headers['X-Turbo-Prefetch']) return true;
        return false;
    }

    function showNavLoader(msg) {
        window.__ofTurboVisitActive = true;
        document.documentElement.classList.remove('of-leaving');
        document.documentElement.classList.add('of-loading');
        if (window.ofLoader) {
            if (loaderShown && typeof window.ofLoader.stay === 'function') {
                window.ofLoader.stay(msg || 'Loading…');
            } else if (typeof window.ofLoader.show === 'function') {
                window.ofLoader.show(msg || 'Loading…');
            }
            loaderShown = true;
        } else {
            const node = document.getElementById('ofLoader');
            if (node) {
                node.classList.add('is-on');
                node.setAttribute('aria-hidden', 'false');
            }
            const label = document.getElementById('ofLoaderLabel');
            if (label && msg) label.textContent = msg;
            loaderShown = true;
        }
        clearSafety();
        safetyTimer = setTimeout(() => hideNavLoader(true), MAX_MS);
    }

    function hideNavLoader(force) {
        clearSafety();
        visitPending = false;
        submitPending = false;
        loaderShown = false;
        window.__ofTurboVisitActive = false;
        document.documentElement.classList.remove('of-loading', 'of-leaving');
        try {
            sessionStorage.removeItem('ofNavLoad');
            sessionStorage.removeItem('ofNavMsg');
        } catch (e) { /* ignore */ }
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
        showNavLoader('Loading…');
    });

    document.addEventListener('turbo:before-visit', () => {
        visitPending = true;
        showNavLoader(submitPending ? 'Saving…' : 'Loading…');
    });

    document.addEventListener('turbo:before-fetch-request', (event) => {
        const opts = event.detail.fetchOptions || {};
        const headers = opts.headers || (opts.headers = {});
        headers.Accept = 'text/html, application/xhtml+xml';
        opts.ofSilent = true;

        // Prefetch on hover must NOT show the loader (nothing clears it)
        if (isPrefetch(opts)) return;

        const method = String(opts.method || 'GET').toUpperCase();
        if (method !== 'GET' && method !== 'HEAD') {
            submitPending = true;
            showNavLoader('Saving…');
            return;
        }
        // Real visit GET already covered by turbo:click / before-visit
        if (visitPending || submitPending) {
            showNavLoader(submitPending ? 'Saving…' : 'Loading…');
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

    document.addEventListener('turbo:submit-start', (event) => {
        submitPending = true;
        const form = event.target;
        let msg = 'Saving…';
        if (form instanceof HTMLFormElement) {
            const action = (form.getAttribute('action') || '').toLowerCase();
            const actionField = form.querySelector('[name="action"]');
            const actionVal = actionField ? String(actionField.value || '') : '';
            if (/delete|remove/.test(action) || actionVal === 'delete') msg = 'Deleting…';
            if (/restore/.test(action) || form.dataset.busyLabel === 'Restoring…') msg = 'Restoring…';
            if (form.dataset.busyLabel) msg = form.dataset.busyLabel;
            if ((form.getAttribute('method') || 'post').toLowerCase() === 'get') msg = 'Loading…';
        }
        showNavLoader(msg);
    });

    document.addEventListener('turbo:submit-end', (event) => {
        const ok = !!(event.detail && event.detail.success);
        if (ok || visitPending) return;
        hideNavLoader(true);
    });

    document.addEventListener('turbo:fetch-request-error', () => {
        hideNavLoader(true);
    });

    document.addEventListener('turbo:visit', () => {
        document.body.classList.remove('sidebar-open');
        const backdrop = document.getElementById('sidebarBackdrop');
        if (backdrop) backdrop.hidden = true;
        document.body.style.overflow = '';
    });

    // Initial turbo:load already fired or about to — ensure clean state after boot
    window.__ofTurboVisitActive = false;
    window.__ofTurboNav = true;
})();
