/**
 * Shared Chart.js helpers — Google Analytics–style lines + doughnut pies.
 */
(function (global) {
    const PALETTE = [
        '#0d9488', '#0284c7', '#e11d48', '#d97706', '#4f46e5',
        '#059669', '#ec4899', '#38bdf8', '#84cc16', '#a855f7',
    ];

    function theme() {
        const dark = document.documentElement.getAttribute('data-theme') === 'dark';
        return {
            dark,
            tick: dark ? '#94a3b8' : '#5b6577',
            grid: dark ? 'rgba(255,255,255,0.06)' : 'rgba(11,18,32,0.06)',
            tooltipBg: dark ? '#121826' : '#0b1220',
            font: "'Sora', 'IBM Plex Sans', system-ui, sans-serif",
            muted: dark ? 'rgba(255,255,255,0.08)' : 'rgba(11,18,32,0.04)',
        };
    }

    function money(n) {
        return 'PKR ' + Number(n || 0).toLocaleString(undefined, {
            minimumFractionDigits: 0,
            maximumFractionDigits: 0,
        });
    }

    function destroyIfAny(canvas) {
        if (!canvas) return;
        const existing = global.Chart?.getChart?.(canvas);
        if (existing) existing.destroy();
    }

    function gaLineChart(canvas, { labels, datasets, yMoney = true }) {
        if (!canvas || !global.Chart) return null;
        destroyIfAny(canvas);
        const t = theme();
        const colored = (datasets || []).map((ds, i) => {
            const color = ds.borderColor || PALETTE[i % PALETTE.length];
            return {
                tension: 0.35,
                fill: true,
                borderWidth: 2.5,
                pointRadius: 0,
                pointHoverRadius: 5,
                pointHoverBorderWidth: 2,
                pointHoverBorderColor: '#fff',
                pointHoverBackgroundColor: color,
                borderColor: color,
                backgroundColor: ds.backgroundColor || (t.dark
                    ? color.replace(')', ', 0.18)').replace('rgb', 'rgba').replace('#', '')
                    : hexToRgba(color, 0.12)),
                ...ds,
                borderColor: color,
                backgroundColor: hexToRgba(color, t.dark ? 0.16 : 0.1),
            };
        });

        return new Chart(canvas, {
            type: 'line',
            data: { labels, datasets: colored },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            color: t.tick,
                            usePointStyle: true,
                            pointStyle: 'circle',
                            padding: 16,
                            font: { family: t.font, size: 12, weight: '600' },
                        },
                    },
                    tooltip: {
                        backgroundColor: t.tooltipBg,
                        titleFont: { family: t.font, weight: '700', size: 13 },
                        bodyFont: { family: t.font, weight: '500', size: 12 },
                        padding: 12,
                        cornerRadius: 10,
                        callbacks: yMoney
                            ? { label: (ctx) => `${ctx.dataset.label}: ${money(ctx.parsed.y)}` }
                            : {},
                    },
                },
                scales: {
                    x: {
                        grid: { display: false },
                        border: { display: false },
                        ticks: {
                            color: t.tick,
                            maxRotation: 0,
                            autoSkip: true,
                            maxTicksLimit: 8,
                            font: { family: t.font, size: 11, weight: '500' },
                        },
                    },
                    y: {
                        beginAtZero: true,
                        border: { display: false },
                        grid: { color: t.grid, drawTicks: false },
                        ticks: {
                            color: t.tick,
                            font: { family: t.font, size: 11, weight: '500' },
                            callback: yMoney
                                ? (v) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(v % 1000 === 0 ? 0 : 1)}k` : v)
                                : undefined,
                        },
                    },
                },
            },
        });
    }

    function doughnutChart(canvas, { labels, values, centerText }) {
        if (!canvas || !global.Chart) return null;
        destroyIfAny(canvas);
        const t = theme();
        const colors = (labels || []).map((_, i) => PALETTE[i % PALETTE.length]);
        return new Chart(canvas, {
            type: 'doughnut',
            data: {
                labels,
                datasets: [{
                    data: values,
                    backgroundColor: colors,
                    borderWidth: 0,
                    hoverOffset: 6,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '68%',
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            color: t.tick,
                            usePointStyle: true,
                            pointStyle: 'circle',
                            padding: 12,
                            boxWidth: 8,
                            font: { family: t.font, size: 11, weight: '600' },
                        },
                    },
                    tooltip: {
                        backgroundColor: t.tooltipBg,
                        titleFont: { family: t.font, weight: '700' },
                        bodyFont: { family: t.font, weight: '500' },
                        padding: 10,
                        cornerRadius: 10,
                        callbacks: {
                            label: (ctx) => {
                                const total = (ctx.dataset.data || []).reduce((a, b) => a + Number(b || 0), 0) || 1;
                                const val = Number(ctx.parsed || 0);
                                const pct = ((val / total) * 100).toFixed(1);
                                return ` ${ctx.label}: ${money(val)} (${pct}%)`;
                            },
                        },
                    },
                },
            },
            plugins: centerText
                ? [{
                    id: 'centerLabel',
                    afterDraw(chart) {
                        const { ctx, chartArea } = chart;
                        if (!chartArea) return;
                        const x = (chartArea.left + chartArea.right) / 2;
                        const y = (chartArea.top + chartArea.bottom) / 2;
                        ctx.save();
                        ctx.textAlign = 'center';
                        ctx.textBaseline = 'middle';
                        ctx.fillStyle = t.tick;
                        ctx.font = `700 11px ${t.font}`;
                        ctx.fillText(centerText.title || '', x, y - 8);
                        ctx.fillStyle = document.documentElement.getAttribute('data-theme') === 'dark' ? '#f6f6f7' : '#1a1c1d';
                        ctx.font = `800 14px ${t.font}`;
                        ctx.fillText(centerText.value || '', x, y + 10);
                        ctx.restore();
                    },
                }]
                : [],
        });
    }

    function hexToRgba(hex, alpha) {
        if (!hex) return `rgba(13,148,136,${alpha})`;
        let h = String(hex).trim();
        if (h.startsWith('rgba') || h.startsWith('rgb')) {
            return h.replace(/rgba?\(([^)]+)\)/, (_, inner) => {
                const parts = inner.split(',').map((p) => p.trim());
                return `rgba(${parts[0]}, ${parts[1]}, ${parts[2]}, ${alpha})`;
            });
        }
        h = h.replace('#', '');
        if (h.length === 3) h = h.split('').map((c) => c + c).join('');
        const n = parseInt(h, 16);
        const r = (n >> 16) & 255;
        const g = (n >> 8) & 255;
        const b = n & 255;
        return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    }

    function parseJsonScript(id) {
        const el = document.getElementById(id);
        if (!el) return null;
        try {
            return JSON.parse(el.textContent);
        } catch (_) {
            return null;
        }
    }

    global.OctaneCharts = {
        gaLineChart,
        doughnutChart,
        parseJsonScript,
        money,
        PALETTE,
        theme,
    };
})(window);
