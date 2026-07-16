document.addEventListener('DOMContentLoaded', () => {
    // Theme Management
    const themeToggleBtn = document.getElementById('theme-toggle');
    const htmlElement = document.documentElement;
    const themeIcon = themeToggleBtn ? themeToggleBtn.querySelector('i') : null;

    const savedTheme = localStorage.getItem('theme') || 'light';
    htmlElement.setAttribute('data-theme', savedTheme);
    updateThemeIcon(savedTheme);

    if (themeToggleBtn) {
        themeToggleBtn.addEventListener('click', () => {
            const currentTheme = htmlElement.getAttribute('data-theme');
            const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
            htmlElement.setAttribute('data-theme', newTheme);
            localStorage.setItem('theme', newTheme);
            updateThemeIcon(newTheme);
        });
    }

    function updateThemeIcon(theme) {
        if (!themeIcon) return;
        themeIcon.className = theme === 'dark' ? 'bi bi-sun-fill' : 'bi bi-moon-stars-fill';
    }

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

    document.addEventListener('submit', (e) => {
        const form = e.target;
        if (!(form instanceof HTMLFormElement)) return;
        if (form.dataset.allowMultiSubmit === '1') return;
        // Already cancelled (e.g. onsubmit="return confirm(...)" → false)
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
            });
            tom.on('change', () => el.dispatchEvent(new Event('change', { bubbles: true })));
        });

        // Keep Tom Select usable inside Bootstrap modals (focus + position)
        document.querySelectorAll('.modal').forEach((modalEl) => {
            modalEl.addEventListener('shown.bs.modal', () => {
                modalEl.querySelectorAll('select.js-search-item, select.js-search-customer').forEach((el) => {
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
