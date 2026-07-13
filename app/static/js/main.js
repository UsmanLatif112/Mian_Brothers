document.addEventListener('DOMContentLoaded', () => {
    // Theme Management
    const themeToggleBtn = document.getElementById('theme-toggle');
    const htmlElement = document.documentElement;
    const themeIcon = themeToggleBtn ? themeToggleBtn.querySelector('i') : null;

    // Load saved theme or default to system preference
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
        if (theme === 'dark') {
            themeIcon.className = 'bi bi-sun-fill';
        } else {
            themeIcon.className = 'bi bi-moon-stars-fill';
        }
    }

    // Searchable selects (Tom Select) — customers by name/phone, items by name
    if (typeof TomSelect !== 'undefined') {
        const shared = {
            maxOptions: null,
            allowEmptyOption: true,
            create: false,
            sortField: { field: 'text', direction: 'asc' },
            render: {
                no_results: () => '<div class="no-results">No matches found</div>'
            }
        };

        document.querySelectorAll('select.js-search-customer').forEach((el) => {
            if (el.tomselect) return;
            const tom = new TomSelect(el, {
                ...shared,
                placeholder: el.dataset.placeholder || 'Search by name or phone...',
                searchField: ['text', 'phone'],
                dropdownParent: el.closest('.modal') ? 'body' : undefined,
            });
            tom.on('change', () => {
                el.dispatchEvent(new Event('change', { bubbles: true }));
            });
        });

        document.querySelectorAll('select.js-search-item').forEach((el) => {
            if (el.tomselect) return;
            const tom = new TomSelect(el, {
                ...shared,
                placeholder: el.dataset.placeholder || 'Search by name...',
                searchField: ['text'],
                dropdownParent: el.closest('.modal') ? 'body' : undefined,
            });
            tom.on('change', () => {
                el.dispatchEvent(new Event('change', { bubbles: true }));
            });
        });
    }

    // Auto-calculate values for Sales forms
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

            if (computedLitersSpan) {
                computedLitersSpan.innerText = litersSold.toFixed(2);
            }

            let rate = 0;
            if (rateInput) {
                rate = parseFloat(rateInput.value) || 0;
            } else if (fuelSelect) {
                const selectedOption = fuelSelect.options[fuelSelect.selectedIndex];
                rate = parseFloat(selectedOption.getAttribute('data-price')) || 0;
            }

            const totalAmount = litersSold * rate;
            if (computedAmountSpan) {
                computedAmountSpan.innerText = totalAmount.toFixed(2);
            }
        };

        openingInput.addEventListener('input', updateCalculations);
        closingInput.addEventListener('input', updateCalculations);
        if (fuelSelect) {
            fuelSelect.addEventListener('change', () => {
                const selectedOption = fuelSelect.options[fuelSelect.selectedIndex];
                const rate = parseFloat(selectedOption.getAttribute('data-price')) || 0;
                if (priceDisplay) {
                    priceDisplay.innerText = rate.toFixed(2);
                }
                updateCalculations();
            });
        }
    }

    // Auto-calculate for custom sales form (Liters to Cost)
    const litersInput = document.getElementById('liters');
    const saleAmountInput = document.getElementById('total_amount_display');
    const saleFuelSelect = document.getElementById('sale_fuel_type_id');

    if (litersInput && saleFuelSelect) {
        const updateSaleAmount = () => {
            const liters = parseFloat(litersInput.value) || 0;
            const selectedOption = saleFuelSelect.options[saleFuelSelect.selectedIndex];
            const rate = parseFloat(selectedOption.getAttribute('data-price')) || 0;
            const total = liters * rate;

            if (saleAmountInput) {
                saleAmountInput.value = total.toFixed(2);
            }
        };

        litersInput.addEventListener('input', updateSaleAmount);
        saleFuelSelect.addEventListener('change', updateSaleAmount);
    }
});
