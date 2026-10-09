/* Global motion loader — orbit + fill; nav resume is handled in base.html (early clear) */
(function initOfLoader() {
    let busy = 0;
    let shownAt = 0;
    let hideTimer = null;
    let safetyTimer = null;
    const MIN_MS = 220;
    const MAX_MS = 6000;
    const NAV_KEY = 'ofNavLoad';
    const NAV_MSG = 'ofNavMsg';

    function el() {
        return document.getElementById('ofLoader');
    }

    function setCopy(message, sub) {
        const label = document.getElementById('ofLoaderLabel');
        const subEl = document.getElementById('ofLoaderSub');
        if (label && message) label.textContent = message;
        if (subEl) subEl.textContent = sub || 'OctaneFlow';
    }

    function kickMotion(node) {
        /* Restart CSS keyframes so car/tank never resume from a paused hidden timeline */
        node.classList.remove('is-on');
        void node.offsetWidth;
        node.classList.add('is-on');
    }

    function paint(on) {
        const node = el();
        if (!node) return;
        if (on) {
            kickMotion(node);
            node.setAttribute('aria-hidden', 'false');
        } else {
            node.classList.remove('is-on');
            node.setAttribute('aria-hidden', 'true');
        }
        document.documentElement.classList.toggle('of-loading', !!on);
    }

    function clearNavFlag() {
        try {
            sessionStorage.removeItem(NAV_KEY);
            sessionStorage.removeItem(NAV_MSG);
        } catch (e) {}
        window.__ofNavPending = false;
    }

    function markNav(message) {
        try {
            sessionStorage.setItem(NAV_KEY, '1');
            sessionStorage.setItem(NAV_MSG, message || 'Loading…');
        } catch (e) {}
    }

    function forceOff() {
        busy = 0;
        clearNavFlag();
        if (hideTimer) clearTimeout(hideTimer);
        if (safetyTimer) clearTimeout(safetyTimer);
        hideTimer = null;
        safetyTimer = null;
        paint(false);
    }

    function armSafety() {
        if (safetyTimer) clearTimeout(safetyTimer);
        safetyTimer = setTimeout(forceOff, MAX_MS);
    }

    function reveal(message, sub) {
        if (hideTimer) {
            clearTimeout(hideTimer);
            hideTimer = null;
        }
        shownAt = Date.now();
        setCopy(message || 'Loading…', sub);
        paint(true);
        armSafety();
    }

    window.ofLoader = {
        show(message, sub) {
            busy += 1;
            reveal(message || 'Working…', sub);
        },
        showNav(message) {
            const msg = message || 'Loading…';
            markNav(msg);
            // Nav overlay is not reference-counted — next page always force-clears
            reveal(msg);
        },
        hide(force) {
            if (force) {
                forceOff();
                return;
            }
            busy = Math.max(0, busy - 1);
            if (busy > 0) return;
            const wait = Math.max(0, MIN_MS - (Date.now() - shownAt));
            if (hideTimer) clearTimeout(hideTimer);
            hideTimer = setTimeout(() => {
                hideTimer = null;
                if (busy === 0) forceOff();
            }, wait);
        },
        wrap(promise, message) {
            this.show(message);
            return Promise.resolve(promise).finally(() => this.hide());
        },
    };

    // Never re-show the nav loader here (that froze animation while Chart.js parsed).
    // Always hard-clear leftovers; in-page CRUD uses show()/hide() after this.
    forceOff();

    // Only mutate requests show loader by default (GET polling must not hang UI)
    const nativeFetch = window.fetch.bind(window);
    window.fetch = function ofFetch(input, init) {
        const opts = init || {};
        const method = String((opts && opts.method) || 'GET').toUpperCase();
        const silent = !!(opts && opts.ofSilent) || method === 'GET' || method === 'HEAD';
        if (!silent) {
            window.ofLoader.show(
                method === 'DELETE' ? 'Deleting…' : 'Saving…'
            );
        }
        const nextInit = Object.assign({}, opts);
        delete nextInit.ofSilent;
        return nativeFetch(input, nextInit).finally(() => {
            if (!silent) window.ofLoader.hide();
        });
    };

    document.addEventListener('click', (e) => {
        const a = e.target.closest && e.target.closest('a[href]');
        if (!a || e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        if (a.target && a.target !== '_self') return;
        if (a.hasAttribute('download')) return;
        if (a.dataset.noLoader === '1') return;
        const href = a.getAttribute('href') || '';
        if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
        try {
            const url = new URL(href, window.location.href);
            if (url.origin !== window.location.origin) return;
            if (url.pathname === window.location.pathname
                && url.search === window.location.search
                && url.hash) return;
        } catch (_) {
            return;
        }
        window.ofLoader.showNav('Loading…');
    }, true);

    window.addEventListener('pageshow', () => {
        forceOff();
    });

    window.addEventListener('load', () => {
        if (busy === 0) forceOff();
    }, { once: true });
})();

/* Modern floating toasts */
(function initOfToasts() {
    const TITLES = {
        success: 'Saved',
        danger: 'Attention',
        warning: 'Notice',
        info: 'Update',
    };
    const ICONS = {
        success: 'bi-check-lg',
        danger: 'bi-exclamation-lg',
        warning: 'bi-exclamation-triangle',
        info: 'bi-info-lg',
    };

    function host() {
        return document.getElementById('ofToastHost');
    }

    function dismissToast(node) {
        if (!node || node.classList.contains('is-leaving')) return;
        node.classList.add('is-leaving');
        setTimeout(() => node.remove(), 280);
    }

    function armToast(node) {
        const closeBtn = node.querySelector('[data-toast-dismiss]');
        closeBtn?.addEventListener('click', () => dismissToast(node));
        const ms = Number(node.dataset.toastMs || 4800);
        const bar = node.querySelector('.of-toast-progress');
        if (bar) bar.style.animationDuration = `${ms}ms`;
        setTimeout(() => dismissToast(node), ms);
    }

    window.ofToast = function ofToast(message, kind, opts) {
        const k = (kind || 'info');
        const options = opts || {};
        const root = host();
        if (!root) return null;
        const node = document.createElement('div');
        node.className = `of-toast of-toast-${k}`;
        node.setAttribute('role', 'status');
        node.dataset.ofToast = '1';
        node.dataset.toastKind = k;
        if (options.ms) node.dataset.toastMs = String(options.ms);
        node.innerHTML = `
            <span class="of-toast-icon" aria-hidden="true"><i class="bi ${ICONS[k] || ICONS.info}"></i></span>
            <div class="of-toast-copy">
                <p class="of-toast-title">${options.title || TITLES[k] || TITLES.info}</p>
                <p class="of-toast-text"></p>
            </div>
            <button type="button" class="of-toast-close" data-toast-dismiss aria-label="Dismiss"><i class="bi bi-x-lg"></i></button>
            <span class="of-toast-progress"></span>`;
        node.querySelector('.of-toast-text').textContent = message || '';
        root.appendChild(node);
        armToast(node);
        return node;
    };

    function boot() {
        document.querySelectorAll('[data-of-toast]').forEach(armToast);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot, { once: true });
    } else {
        boot();
    }
})();

/* In-app confirm modal — replaces window.confirm() */
(function initOfConfirm() {
    let resolvePromise = null;
    let modalInstance = null;

    function getModal() {
        const el = document.getElementById('ofConfirmModal');
        if (!el || typeof bootstrap === 'undefined') return null;
        if (!modalInstance) modalInstance = bootstrap.Modal.getOrCreateInstance(el);
        return { el, modal: modalInstance };
    }

    /**
     * @param {object|string} opts message string, or { title, message, confirmLabel, cancelLabel, danger, alertOnly }
     * @returns {Promise<boolean>}
     */
    window.ofConfirm = function ofConfirm(opts) {
        const options = typeof opts === 'string' ? { message: opts } : (opts || {});
        const title = options.title || 'Confirm';
        const message = options.message || 'Are you sure?';
        const alertOnly = !!options.alertOnly;
        const confirmLabel = options.confirmLabel
            || (alertOnly ? 'OK' : (options.danger === false ? 'Confirm' : 'Delete'));
        const cancelLabel = options.cancelLabel || 'Cancel';
        const danger = alertOnly ? false : options.danger !== false;

        const ctx = getModal();
        if (!ctx) {
            if (alertOnly) {
                window.alert(message);
                return Promise.resolve(true);
            }
            return Promise.resolve(window.confirm(message));
        }

        const { el, modal } = ctx;
        const titleEl = el.querySelector('#ofConfirmTitle');
        const msgEl = el.querySelector('#ofConfirmMessage');
        const okBtn = el.querySelector('#ofConfirmOk');
        const cancelBtn = el.querySelector('#ofConfirmCancel');

        if (titleEl) titleEl.textContent = title;
        if (msgEl) msgEl.textContent = message;
        if (okBtn) {
            okBtn.textContent = confirmLabel;
            okBtn.className = danger
                ? 'btn btn-danger px-3'
                : 'btn btn-primary-custom px-3';
        }
        if (cancelBtn) {
            cancelBtn.textContent = cancelLabel;
            cancelBtn.classList.toggle('d-none', alertOnly);
        }

        return new Promise((resolve) => {
            resolvePromise = resolve;
            modal.show();
        });
    };

    /** Info / block alert (single OK). */
    window.ofAlert = function ofAlert(opts) {
        const options = typeof opts === 'string' ? { message: opts } : (opts || {});
        return window.ofConfirm({
            title: options.title || 'Notice',
            message: options.message || '',
            confirmLabel: options.confirmLabel || 'OK',
            alertOnly: true,
            danger: false,
        });
    };

    document.addEventListener('DOMContentLoaded', () => {
        const el = document.getElementById('ofConfirmModal');
        if (!el) return;
        const okBtn = el.querySelector('#ofConfirmOk');
        okBtn?.addEventListener('click', () => {
            const ctx = getModal();
            const done = resolvePromise;
            resolvePromise = null;
            ctx?.modal.hide();
            if (done) done(true);
        });
        el.addEventListener('hidden.bs.modal', () => {
            if (resolvePromise) {
                const done = resolvePromise;
                resolvePromise = null;
                done(false);
            }
        });
    });

    // Forms: data-block-delete="…" blocks submit with alert; data-confirm="…" asks first
    document.addEventListener('submit', (e) => {
        const form = e.target;
        if (!(form instanceof HTMLFormElement)) return;

        const blockMsg = form.getAttribute('data-block-delete');
        if (blockMsg) {
            e.preventDefault();
            e.stopImmediatePropagation();
            window.ofAlert({
                title: form.getAttribute('data-block-title') || 'Cannot delete',
                message: blockMsg,
            });
            return;
        }

        const message = form.getAttribute('data-confirm');
        if (!message) return;
        if (form.dataset.ofConfirmed === '1') {
            delete form.dataset.ofConfirmed;
            return;
        }

        e.preventDefault();
        e.stopImmediatePropagation();

        const title = form.getAttribute('data-confirm-title') || 'Confirm';
        const confirmLabel = form.getAttribute('data-confirm-btn') || 'Delete';
        const danger = form.getAttribute('data-confirm-danger') !== '0';

        window.ofConfirm({ title, message, confirmLabel, danger }).then((ok) => {
            if (!ok) return;
            form.dataset.ofConfirmed = '1';
            if (typeof form.requestSubmit === 'function') {
                form.requestSubmit();
            } else {
                HTMLFormElement.prototype.submit.call(form);
            }
        });
    }, true);
})();

document.addEventListener('DOMContentLoaded', () => {
    // Surface "Cannot delete…" toasts as an in-app alert popup
    const blockFlash = document.querySelector('.of-toast-danger .of-toast-text');
    if (blockFlash && /cannot delete/i.test(blockFlash.textContent || '')) {
        const text = (blockFlash.textContent || '').replace(/\s+/g, ' ').trim();
        window.ofAlert({ title: 'Cannot delete', message: text });
    }

    // Theme Management
    const themeToggleBtn = document.getElementById('theme-toggle');
    const htmlElement = document.documentElement;

    const savedTheme = localStorage.getItem('theme') || 'light';
    htmlElement.setAttribute('data-theme', savedTheme);
    updateThemeToggle(savedTheme);

    if (themeToggleBtn) {
        themeToggleBtn.addEventListener('click', () => {
            const currentTheme = htmlElement.getAttribute('data-theme');
            const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
            htmlElement.setAttribute('data-theme', newTheme);
            localStorage.setItem('theme', newTheme);
            updateThemeToggle(newTheme);
        });
    }

    function updateThemeToggle(theme) {
        if (!themeToggleBtn) return;
        const isDark = theme === 'dark';
        if (themeToggleBtn.classList.contains('dropdown-item')) {
            themeToggleBtn.innerHTML = isDark
                ? '<i class="bi bi-sun-fill me-2"></i> Light Mode'
                : '<i class="bi bi-moon-stars-fill me-2"></i> Dark Mode';
            return;
        }
        const icon = themeToggleBtn.querySelector('i');
        if (icon) icon.className = isDark ? 'bi bi-sun-fill' : 'bi bi-moon-stars-fill';
    }

    // -------------------------------------------------------
    // Shopify-style shell: sidebar + global search
    // -------------------------------------------------------
    const body = document.body;
    const sidebar = document.getElementById('appSidebar');
    const backdrop = document.getElementById('sidebarBackdrop');
    const openBtn = document.getElementById('sidebarOpen');
    const closeBtn = document.getElementById('sidebarClose');

    function openSidebar() {
        if (!sidebar) return;
        body.classList.add('sidebar-open');
        if (backdrop) backdrop.hidden = false;
        body.style.overflow = window.innerWidth < 992 ? 'hidden' : '';
    }

    function closeSidebar() {
        body.classList.remove('sidebar-open');
        if (backdrop) backdrop.hidden = true;
        body.style.overflow = '';
    }

    if (openBtn) openBtn.addEventListener('click', openSidebar);
    if (closeBtn) closeBtn.addEventListener('click', closeSidebar);
    if (backdrop) backdrop.addEventListener('click', closeSidebar);

    sidebar?.querySelectorAll('.sidebar-link').forEach((link) => {
        link.addEventListener('click', () => {
            if (window.innerWidth < 992) closeSidebar();
        });
    });

    window.addEventListener('resize', () => {
        if (window.innerWidth >= 992) closeSidebar();
    });

    const searchInput = document.getElementById('globalSearch') || document.querySelector('.topbar-search-input');
    const searchPanel = document.getElementById('searchPanel');
    const searchResults = document.getElementById('searchResults');
    const searchForm = document.getElementById('globalSearchForm') || document.querySelector('.topbar-search');
    const isGlobalSearch = searchForm?.dataset?.searchMode === 'global';
    const navLinks = Array.from(document.querySelectorAll('.sidebar-link[data-search-label]')).map((el) => ({
        label: el.getAttribute('data-search-label') || el.textContent.trim(),
        href: el.getAttribute('href'),
        icon: el.querySelector('i')?.className || 'bi bi-arrow-right',
    }));

    function renderSearch(query) {
        if (!isGlobalSearch || !searchPanel || !searchResults) return;
        const q = (query || '').trim().toLowerCase();
        if (!q) {
            searchPanel.hidden = true;
            searchResults.innerHTML = '';
            return;
        }
        const matches = navLinks.filter((item) => item.label.toLowerCase().includes(q)).slice(0, 8);
        const customerJump = {
            label: `Search customers for “${query.trim()}”`,
            href: `${searchForm?.action || '/customers/'}?search=${encodeURIComponent(query.trim())}`,
            icon: 'bi bi-people',
        };
        const vendorJump = {
            label: `Search vendors for “${query.trim()}”`,
            href: `/vendors/?search=${encodeURIComponent(query.trim())}`,
            icon: 'bi bi-building',
        };
        const items = [...matches, customerJump, vendorJump];
        searchResults.innerHTML = items
            .map(
                (item, idx) =>
                    `<a class="topbar-search-item${idx === 0 ? ' is-active' : ''}" href="${item.href}">
                        <i class="${item.icon}"></i><span>${item.label}</span>
                     </a>`
            )
            .join('');
        searchPanel.hidden = false;
    }

    if (searchInput && isGlobalSearch) {
        searchInput.addEventListener('input', () => renderSearch(searchInput.value));
        searchInput.addEventListener('focus', () => {
            if (searchInput.value.trim()) renderSearch(searchInput.value);
        });
        searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                searchPanel.hidden = true;
                searchInput.blur();
            }
        });
        document.addEventListener('click', (e) => {
            if (!searchForm?.contains(e.target) && searchPanel) searchPanel.hidden = true;
        });
    }

    document.addEventListener('keydown', (e) => {
        if (e.key === '/' && !e.metaKey && !e.ctrlKey && !e.altKey) {
            const tag = (document.activeElement?.tagName || '').toLowerCase();
            if (tag === 'input' || tag === 'textarea' || document.activeElement?.isContentEditable) return;
            if (!searchInput) return;
            e.preventDefault();
            searchInput.focus();
            searchInput.select();
        }
    });

    // -------------------------------------------------------
    // Prevent duplicate submits / double-clicks (global)
    // -------------------------------------------------------
    function getFormSubmitButtons(form) {
        return Array.from(
            form.querySelectorAll('button[type="submit"], input[type="submit"]')
        );
    }

    function lockSubmitButton(btn, label) {
        if (!btn || btn.dataset.locked === '1') return;
        btn.dataset.locked = '1';
        if (btn.dataset.originalDisabled == null) {
            btn.dataset.originalDisabled = btn.disabled ? '1' : '0';
        }
        if (!btn.dataset.originalHtml) {
            btn.dataset.originalHtml = btn.tagName === 'INPUT' ? btn.value : btn.innerHTML;
        }
        btn.disabled = true;
        btn.setAttribute('aria-busy', 'true');
        btn.classList.add('is-submitting');
        if (btn.tagName === 'INPUT') {
            btn.value = label;
        } else {
            btn.innerHTML =
                `<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>${label}`;
        }
    }

    function unlockSubmitButton(btn) {
        if (!btn) return;
        btn.disabled = btn.dataset.originalDisabled === '1';
        btn.classList.remove('is-submitting');
        btn.removeAttribute('aria-busy');
        delete btn.dataset.locked;
        delete btn.dataset.originalDisabled;
        if (btn.dataset.originalHtml != null) {
            if (btn.tagName === 'INPUT') btn.value = btn.dataset.originalHtml;
            else btn.innerHTML = btn.dataset.originalHtml;
            delete btn.dataset.originalHtml;
        }
    }

    function lockFormButtons(form, busyLabel) {
        const label = busyLabel || form.dataset.busyLabel || 'Saving...';
        getFormSubmitButtons(form).forEach((btn) => {
            const isFilterBtn = btn.classList.contains('period-filter-submit');
            lockSubmitButton(btn, isFilterBtn ? '…' : label);
        });
    }

    function unlockForm(form) {
        if (!form) return;
        delete form.dataset.submitting;
        getFormSubmitButtons(form).forEach(unlockSubmitButton);
    }

    window.lockSubmitButton = lockSubmitButton;
    window.unlockSubmitButton = unlockSubmitButton;
    window.unlockFormSubmit = unlockForm;

    function showFormLoader(form) {
        if (!form || form.dataset.noLoader === '1' || !window.ofLoader) return;
        const action = (form.getAttribute('action') || '').toLowerCase();
        const method = (form.getAttribute('method') || 'post').toLowerCase();
        const actionField = form.querySelector('[name="action"]');
        const actionVal = actionField ? String(actionField.value || '') : '';
        let msg = 'Saving…';
        if (method === 'get' || form.dataset.allowMultiSubmit === '1') msg = 'Loading…';
        if (/delete|remove/.test(action) || actionVal === 'delete') msg = 'Deleting…';
        if (form.dataset.busyLabel) msg = form.dataset.busyLabel;
        // Full POST/GET navigations keep the loader on the next page
        if (method !== 'dialog') window.ofLoader.showNav(msg);
        else window.ofLoader.show(msg);
    }

    document.addEventListener('submit', (e) => {
        const form = e.target;
        if (!(form instanceof HTMLFormElement)) return;

        // AJAX Register Vendor / Customer — handled below (do not full-page navigate)
        if (form.dataset.ofAjaxCreate) return;

        // Filter / multi-submit forms: still show loader, allow resubmit
        if (form.dataset.allowMultiSubmit === '1') {
            if (!e.defaultPrevented) showFormLoader(form);
            return;
        }
        // Already cancelled (e.g. in-app confirm dismissed)
        if (e.defaultPrevented) return;

        // Block immediate double-submit
        if (form.dataset.submitting === '1') {
            e.preventDefault();
            e.stopPropagation();
            return;
        }

        form.dataset.submitting = '1';

        // Defer disable so the clicked submitter still serializes into the request
        setTimeout(() => {
            if (e.defaultPrevented) {
                unlockForm(form);
                return;
            }
            lockFormButtons(form);
            showFormLoader(form);
        }, 0);
    });

    // Programmatic form.submit() does not fire the submit event — cover it too
    const nativeFormSubmit = HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit = function patchedFormSubmit() {
        if (this.dataset.allowMultiSubmit === '1') {
            return nativeFormSubmit.call(this);
        }
        if (this.dataset.submitting === '1') return;
        this.dataset.submitting = '1';
        lockFormButtons(this);
        showFormLoader(this);
        return nativeFormSubmit.call(this);
    };

    async function postJson(url, body) {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        return res.json();
    }

    /* —— Plus-button Register Vendor / Customer (AJAX + TomSelect cascade) —— */
    let ofQuickSelectTarget = null;

    document.addEventListener('click', (e) => {
        const btn = e.target.closest && e.target.closest('[data-of-select-target]');
        if (!btn) return;
        const sel = document.querySelector(btn.getAttribute('data-of-select-target'));
        ofQuickSelectTarget = sel || null;
    }, true);

    function ofAddOptionToSelects(selector, option) {
        const id = String(option.id);
        document.querySelectorAll(selector).forEach((el) => {
            const tom = el.tomselect;
            if (tom) {
                if (!tom.options[id]) {
                    tom.addOption({
                        value: id,
                        text: option.text,
                        phone: option.phone || '',
                    });
                } else {
                    tom.updateOption(id, {
                        value: id,
                        text: option.text,
                        phone: option.phone || '',
                    });
                }
                tom.refreshOptions(false);
            } else {
                let opt = el.querySelector(`option[value="${CSS.escape(id)}"]`);
                if (!opt) {
                    opt = document.createElement('option');
                    opt.value = id;
                    el.appendChild(opt);
                }
                opt.textContent = option.text;
                if (option.phone) opt.dataset.phone = option.phone;
            }
        });
    }

    function ofSelectNewOption(el, option) {
        if (!el) return;
        const id = String(option.id);
        if (el.tomselect) {
            if (!el.tomselect.options[id]) {
                el.tomselect.addOption({
                    value: id,
                    text: option.text,
                    phone: option.phone || '',
                });
            }
            el.tomselect.setValue(id, true);
        } else {
            el.value = id;
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }

    function ofStackQuickModal(modalEl) {
        if (!modalEl) return;
        const openCount = document.querySelectorAll('.modal.show').length;
        if (openCount <= 1) return;
        const z = 1055 + openCount * 20;
        modalEl.style.zIndex = String(z);
        setTimeout(() => {
            const backs = document.querySelectorAll('.modal-backdrop');
            const last = backs[backs.length - 1];
            if (last) last.style.zIndex = String(z - 5);
        }, 0);
    }

    document.addEventListener('show.bs.modal', (e) => {
        if (e.target && e.target.classList.contains('of-quick-create-modal')) {
            ofStackQuickModal(e.target);
        }
    });

    document.addEventListener('submit', async (e) => {
        const form = e.target;
        if (!(form instanceof HTMLFormElement) || !form.dataset.ofAjaxCreate) return;
        e.preventDefault();
        e.stopPropagation();

        const kind = form.dataset.ofAjaxCreate;
        const url = form.dataset.createUrl;
        if (!url) return;

        if (form.dataset.submitting === '1') return;
        form.dataset.submitting = '1';

        const submitBtn = form.querySelector('[type="submit"]');
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.dataset._label = submitBtn.innerHTML;
            submitBtn.innerHTML = 'Saving…';
        }

        const payload = Object.fromEntries(new FormData(form).entries());
        try {
            const data = await postJson(url, payload);
            if (!data || !data.ok) {
                if (window.ofToast) window.ofToast(data && data.error ? data.error : 'Could not save', 'danger');
                else alert((data && data.error) || 'Could not save');
                return;
            }

            const selectSel = kind === 'vendor' ? 'select.js-search-vendor' : 'select.js-search-customer';
            const matches = document.querySelectorAll(selectSel);
            if (matches.length) {
                ofAddOptionToSelects(selectSel, data);
                ofSelectNewOption(ofQuickSelectTarget || matches[0], data);
                if (window.ofToast) {
                    window.ofToast(
                        kind === 'vendor' ? 'Vendor registered' : 'Customer registered',
                        'success'
                    );
                }
                const modalEl = form.closest('.modal');
                if (modalEl && window.bootstrap) {
                    bootstrap.Modal.getOrCreateInstance(modalEl).hide();
                }
                form.reset();
            } else {
                // Standalone vendors/customers page — refresh list
                window.location.reload();
                return;
            }
        } catch (err) {
            if (window.ofToast) window.ofToast('Could not save. Try again.', 'danger');
            else alert('Could not save. Try again.');
        } finally {
            delete form.dataset.submitting;
            if (submitBtn) {
                submitBtn.disabled = false;
                if (submitBtn.dataset._label) {
                    submitBtn.innerHTML = submitBtn.dataset._label;
                    delete submitBtn.dataset._label;
                }
            }
        }
    });

    if (typeof TomSelect !== 'undefined') {
        const shared = {
            maxOptions: null,          // show all matches; list scrolls
            allowEmptyOption: true,
            openOnFocus: true,         // open list when field is clicked/focused
            closeAfterSelect: true,
            hideSelected: false,
            searchField: ['text'],
            sortField: { field: 'text', direction: 'asc' },
            render: {
                no_results: (data, escape) =>
                    `<div class="no-results">No matches for “${escape(data.input)}”</div>`,
                option_create: (data, escape) =>
                    `<div class="create">Add <strong>${escape(data.input)}</strong>…</div>`,
            },
        };

        const dropdownParentFor = (el) => (el.closest('.modal') ? 'body' : undefined);

        /** Visible › chevron: points down closed, up when open */
        const attachSelectCaret = (wrapper) => {
            if (!wrapper || wrapper.querySelector('.of-select-caret')) return;
            wrapper.classList.add('of-select');
            const caret = document.createElement('span');
            caret.className = 'of-select-caret';
            caret.setAttribute('aria-hidden', 'true');
            wrapper.appendChild(caret);
        };

        document.querySelectorAll('select.js-search-customer').forEach((el) => {
            if (el.tomselect) return;
            const createUrl = el.dataset.createUrl;
            const canCreate = el.classList.contains('js-create-customer') && createUrl;
            const tom = new TomSelect(el, {
                ...shared,
                placeholder: el.dataset.placeholder || 'Type to search customer...',
                searchField: ['text', 'phone'],
                dropdownParent: dropdownParentFor(el),
                create: canCreate
                    ? (input, callback) => {
                          const phone = window.prompt(`Phone for "${input}" (optional):`, '') || '';
                          postJson(createUrl, { name: input.trim(), phone: phone.trim() })
                              .then((data) => {
                                  if (!data.ok) {
                                      alert(data.error || 'Could not add customer');
                                      callback();
                                      return;
                                  }
                                  callback({
                                      value: String(data.id),
                                      text: data.text,
                                      phone: data.phone || '',
                                  });
                              })
                              .catch(() => {
                                  alert('Could not add customer');
                                  callback();
                              });
                      }
                    : false,
                onInitialize() {
                    this.wrapper.classList.add('of-select', 'of-select-search');
                    attachSelectCaret(this.wrapper);
                },
            });
            tom.on('change', () => el.dispatchEvent(new Event('change', { bubbles: true })));
        });

        document.querySelectorAll('select.js-search-vendor').forEach((el) => {
            if (el.tomselect) return;
            const createUrl = el.dataset.createUrl;
            const canCreate = el.classList.contains('js-create-vendor') && createUrl;
            const tom = new TomSelect(el, {
                ...shared,
                placeholder: el.dataset.placeholder || 'Type to search vendor...',
                searchField: ['text', 'phone'],
                dropdownParent: dropdownParentFor(el),
                create: canCreate
                    ? (input, callback) => {
                          const phone = window.prompt(`Phone for "${input}" (optional):`, '') || '';
                          postJson(createUrl, { name: input.trim(), phone: phone.trim() })
                              .then((data) => {
                                  if (!data.ok) {
                                      alert(data.error || 'Could not add vendor');
                                      callback();
                                      return;
                                  }
                                  callback({
                                      value: String(data.id),
                                      text: data.text,
                                      phone: data.phone || '',
                                  });
                              })
                              .catch(() => {
                                  alert('Could not add vendor');
                                  callback();
                              });
                      }
                    : false,
                render: {
                    ...shared.render,
                    option_create: (data, escape) =>
                        `<div class="create">Add vendor <strong>${escape(data.input)}</strong>…</div>`,
                },
                onInitialize() {
                    this.wrapper.classList.add('of-select', 'of-select-search');
                    attachSelectCaret(this.wrapper);
                },
            });
            tom.on('change', () => el.dispatchEvent(new Event('change', { bubbles: true })));
        });

        document.querySelectorAll('select.js-search-item').forEach((el) => {
            if (el.tomselect) return;
            const createUrl = el.dataset.createUrl;
            const createFuel = el.classList.contains('js-create-fuel') && createUrl;
            const createItem = el.classList.contains('js-create-item') && createUrl;

            const tom = new TomSelect(el, {
                ...shared,
                placeholder: el.dataset.placeholder || 'Type to search...',
                searchField: ['text'],
                dropdownParent: dropdownParentFor(el),
                create: createFuel || createItem
                    ? (input, callback) => {
                          if (createFuel) {
                              postJson(createUrl, { name: input.trim() })
                                  .then((data) => {
                                      if (!data.ok) {
                                          alert(data.error || 'Could not add fuel');
                                          callback();
                                          return;
                                      }
                                      // Sales page needs reload for machine blocks; inventory can keep the form open
                                      if (el.dataset.noReload === '1') {
                                          callback({
                                              value: String(data.id || data.value),
                                              text: data.text || data.name,
                                              rate: data.rate,
                                              stock: data.stock,
                                          });
                                          return;
                                      }
                                      location.reload();
                                  })
                                  .catch(() => {
                                      alert('Could not add fuel');
                                      callback();
                                  });
                              return;
                          }
                          const priceRaw = window.prompt(`Sale price (PKR) for "${input}":`, '');
                          const sale_price = parseFloat(priceRaw || '0') || 0;
                          postJson(createUrl, { name: input.trim(), sale_price })
                              .then((data) => {
                                  if (!data.ok) {
                                      alert(data.error || 'Could not add item');
                                      callback();
                                      return;
                                  }
                                  callback({
                                      value: data.value,
                                      text: data.text,
                                      rate: data.rate,
                                      unit: 'qty',
                                      stock: 0,
                                  });
                              })
                              .catch(() => {
                                  alert('Could not add item');
                                  callback();
                              });
                      }
                    : false,
                render: {
                    ...shared.render,
                    ...(createFuel
                        ? {
                              option_create: (data, escape) =>
                                  `<div class="create">Add fuel type <strong>${escape(data.input)}</strong>…</div>`,
                          }
                        : {}),
                },
                onInitialize() {
                    this.wrapper.classList.add('of-select', 'of-select-search');
                    attachSelectCaret(this.wrapper);
                },
            });
            tom.on('change', () => el.dispatchEvent(new Event('change', { bubbles: true })));
        });

        // Plain selects (filters + modal fields) — same custom UI, no native OS chrome
        document.querySelectorAll('select.form-select').forEach((el) => {
            if (el.tomselect) return;

            const isFilter =
                el.classList.contains('topbar-filter-select') ||
                el.classList.contains('period-select') ||
                el.classList.contains('topbar-filter-type');
            const inModal = !!el.closest('.modal');
            const optionCount = el.options ? el.options.length : 0;
            const searchable = !isFilter && optionCount > 10;

            new TomSelect(el, {
                maxOptions: null,
                allowEmptyOption: true,
                create: false,
                controlInput: searchable ? undefined : null,
                openOnFocus: true,
                closeAfterSelect: true,
                hideSelected: false,
                searchField: ['text'],
                dropdownParent: inModal ? 'body' : undefined,
                dropdownClass: isFilter ? 'ts-dropdown of-select-filter-menu' : 'ts-dropdown',
                placeholder: el.dataset.placeholder || el.getAttribute('placeholder') || '',
                render: {
                    no_results: (data, escape) =>
                        `<div class="no-results">No matches for “${escape(data.input)}”</div>`,
                },
                onInitialize() {
                    this.wrapper.classList.add('of-select');
                    if (isFilter) this.wrapper.classList.add('of-select-filter');
                    if (inModal) this.wrapper.classList.add('of-select-modal');
                    attachSelectCaret(this.wrapper);
                },
                onDropdownOpen() {
                    if (!isFilter || !this.dropdown) return;
                    // Fit labels on one line — don't inherit the narrow control width
                    this.dropdown.style.minWidth = '13rem';
                    this.dropdown.style.width = 'max-content';
                    this.dropdown.style.maxWidth = 'min(90vw, 18rem)';
                },
            });
        });

        // Keep Tom Select usable inside Bootstrap modals (focus + position)
        document.querySelectorAll('.modal').forEach((modalEl) => {
            modalEl.addEventListener('shown.bs.modal', () => {
                modalEl.querySelectorAll('select.form-select').forEach((el) => {
                    if (!el.tomselect) return;
                    el.tomselect.positionDropdown();
                });
            });
        });
    }

    // Legacy meter calc helpers (older forms)
    const openingInput = document.getElementById('opening_reading');
    const closingInput = document.getElementById('closing_reading');
    const computedLitersSpan = document.getElementById('computed_liters');
    const computedAmountSpan = document.getElementById('computed_amount');
    const fuelSelect = document.getElementById('fuel_type_id');
    const priceDisplay = document.getElementById('price_display');
    const rateInput = document.getElementById('price_per_liter_input');

    if (openingInput && closingInput) {
        const updateCalculations = () => {
            const openVal = parseFloat(openingInput.value) || 0;
            const closeVal = parseFloat(closingInput.value) || 0;
            const litersSold = Math.max(0, closeVal - openVal);
            if (computedLitersSpan) computedLitersSpan.innerText = litersSold.toFixed(2);
            let rate = 0;
            if (rateInput) rate = parseFloat(rateInput.value) || 0;
            else if (fuelSelect) {
                const selectedOption = fuelSelect.options[fuelSelect.selectedIndex];
                rate = parseFloat(selectedOption?.getAttribute('data-price')) || 0;
            }
            if (computedAmountSpan) computedAmountSpan.innerText = (litersSold * rate).toFixed(2);
        };
        openingInput.addEventListener('input', updateCalculations);
        closingInput.addEventListener('input', updateCalculations);
        if (fuelSelect) fuelSelect.addEventListener('change', updateCalculations);
    }
});
