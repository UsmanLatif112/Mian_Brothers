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
            maxOptions: null,
            allowEmptyOption: true,
            sortField: { field: 'text', direction: 'asc' },
            render: {
                no_results: (data, escape) =>
                    `<div class="no-results">No matches for “${escape(data.input)}”</div>`,
                option_create: (data, escape) =>
                    `<div class="create">Add <strong>${escape(data.input)}</strong>…</div>`,
            },
        };

        document.querySelectorAll('select.js-search-customer').forEach((el) => {
            if (el.tomselect) return;
            const createUrl = el.dataset.createUrl;
            const canCreate = el.classList.contains('js-create-customer') && createUrl;
            const tom = new TomSelect(el, {
                ...shared,
                placeholder: el.dataset.placeholder || 'Search by name or phone...',
                searchField: ['text', 'phone'],
                dropdownParent: el.closest('.modal') ? 'body' : undefined,
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
                placeholder: el.dataset.placeholder || 'Search by name...',
                searchField: ['text'],
                dropdownParent: el.closest('.modal') ? 'body' : undefined,
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
                render: createFuel
                    ? {
                          option_create: (data, escape) =>
                              `<div class="create">Add fuel type <strong>${escape(data.input)}</strong>…</div>`,
                      }
                    : undefined,
            });
            tom.on('change', () => el.dispatchEvent(new Event('change', { bubbles: true })));
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
