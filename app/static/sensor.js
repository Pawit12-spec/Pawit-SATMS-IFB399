document.addEventListener('DOMContentLoaded', function () {
    var page = document.getElementById('sensor-page');
    if (!page) return;

    var field     = page.dataset.field;
    var color     = page.dataset.color;
    var units     = page.dataset.units;
    var baseTitle = page.dataset.chartTitle || 'Sensor';

    var currentPeriod = 'day';
    var periodSelect  = document.getElementById('periodSelect');
    var chartTitleEl  = document.querySelector('.chart-title');

    var gridColor = 'rgba(160,160,160,0.15)';
    var tickColor = 'rgba(160,160,160,0.7)';

    var chart = new Chart(document.getElementById('sensorChart'), {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                data: [],
                borderWidth: 2,
                tension: 0.35,
                fill: true,
                backgroundColor: color + '18',
                borderColor: color,
                pointRadius: 0,
                pointHoverRadius: 5,
                pointHoverBackgroundColor: color,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300, easing: 'easeInOutQuart' },
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: {
                    mode: 'index',
                    intersect: false,
                    callbacks: {
                        label: function (ctx) {
                            return ctx.parsed.y.toFixed(1) + ' ' + units;
                        }
                    }
                }
            },
            scales: {
                x: {
                    display: true,
                    ticks: { color: tickColor, font: { size: 11 }, maxTicksLimit: 8, maxRotation: 0 },
                    grid: { color: gridColor },
                    border: { display: false },
                },
                y: {
                    display: true,
                    ticks: {
                        color: tickColor,
                        font: { size: 11 },
                        maxTicksLimit: 6,
                        callback: function (v) { return v + ' ' + units; }
                    },
                    grid: { color: gridColor },
                    border: { display: false },
                }
            }
        }
    });

    var tableBody = document.getElementById('sensorTableBody');
    var MAX_TABLE_ROWS = 50;

    function escapeHtml(str) {
        var d = document.createElement('div');
        d.textContent = str;
        return d.innerHTML;
    }

    var thresholds = {
        temperature_c: { low: 15, high: 40 },
        humidity_pct:  { low: 30, high: 70 },
        co2_ppm:       { low: 0,  high: 1000 },
        aqi:           { low: 0,  high: 50 }
    };

    function statusFor(avg) {
        var t = thresholds[field] || { low: -Infinity, high: Infinity };
        if (avg >= t.low && avg <= t.high) return { label: 'Normal', cls: 'status-normal' };
        return { label: 'Warning', cls: 'status-warning' };
    }

    function renderTable(rows) {
        tableBody.innerHTML = '';
        rows.forEach(function (r) {
            var st = statusFor(r.avg);
            var tr = document.createElement('tr');
            tr.innerHTML =
                '<td>' + escapeHtml(r.time) + '</td>' +
                '<td class="val-cell">' + escapeHtml(formatValue(r.avg) + ' ' + units) + '</td>' +
                '<td class="val-cell">' + escapeHtml(formatValue(r.min) + ' ' + units) + '</td>' +
                '<td class="val-cell">' + escapeHtml(formatValue(r.max) + ' ' + units) + '</td>' +
                '<td class="val-cell">' + escapeHtml(String(r.samples)) + '</td>' +
                '<td><span class="status-badge ' + st.cls + '">' + st.label + '</span></td>';
            tableBody.appendChild(tr);
        });
    }

    /* ---- format helpers per period ---- */
    function formatTime(isoStr, period) {
        var d = new Date(isoStr);
        var hh = d.getHours().toString().padStart(2, '0');
        var mm = d.getMinutes().toString().padStart(2, '0');
        var ss = d.getSeconds().toString().padStart(2, '0');
        var dd = d.getDate().toString().padStart(2, '0');
        var mon = (d.getMonth() + 1).toString().padStart(2, '0');

        if (period === 'day')   return hh + ':' + mm + ':' + ss;
        if (period === 'week')  return dd + '/' + mon + ' ' + hh + ':' + mm;
        if (period === 'month') return dd + '/' + mon + ' ' + hh + ':' + mm;
        return dd + '/' + mon + '/' + d.getFullYear();
    }

    function formatValue(val) {
        return (field === 'humidity_pct' || field === 'co2_ppm')
            ? Math.round(val).toString()
            : Number(val).toFixed(1);
    }

    var chartLoading = document.getElementById('chartLoading');

    function showLoading() { if (chartLoading) chartLoading.classList.add('active'); }
    function hideLoading() { if (chartLoading) chartLoading.classList.remove('active'); }

    /* Historical fetch */
    function loadPeriod(period) {
        currentPeriod = period;
        var titles = {
            day:   baseTitle + ' Throughout the Day',
            week:  baseTitle + ' Throughout the Week',
            month: baseTitle + ' Throughout the Month',
            year:  baseTitle + ' Throughout the Year'
        };
        if (chartTitleEl) chartTitleEl.textContent = titles[period] || '';

        showLoading();

        fetch('/api/readings?period=' + encodeURIComponent(period) + '&field=' + encodeURIComponent(field))
            .then(function (res) { return res.json(); })
            .then(function (data) {
                var rows = data.readings || [];

                /* update chart */
                chart.data.labels = rows.map(function (r) { return formatTime(r.time, period); });
                chart.data.datasets[0].data = rows.map(function (r) { return r.value; });
                chart.data.datasets[0].pointRadius = rows.length > 200 ? 0 : 2;
                chart.update('none');

                /* update table — show latest first, limited */
                var tblData = (data.table || []).slice().reverse().slice(0, MAX_TABLE_ROWS).map(function (r) {
                    return {
                        time: formatTime(r.time, period),
                        avg: r.avg, min: r.min, max: r.max, samples: r.samples
                    };
                });
                renderTable(tblData);
            })
            .catch(function (err) {
                console.warn('Failed to load readings:', err);
            })
            .finally(function () {
                hideLoading();
            });
    }

    if (periodSelect) {
        periodSelect.addEventListener('change', function () {
            loadPeriod(this.value);
        });
    }

    loadPeriod('day');

    /* auto-refresh: day=60s, week=5min, month/year=10min  */
    setInterval(function () {
        var interval = { day: 60, week: 300, month: 600, year: 600 };
        if (!window._lastRefresh) window._lastRefresh = {};
        var now = Date.now();
        var due = interval[currentPeriod] * 1000;
        if (!window._lastRefresh[currentPeriod] || now - window._lastRefresh[currentPeriod] >= due) {
            window._lastRefresh[currentPeriod] = now;
            loadPeriod(currentPeriod);
        }
    }, 15000);

    /* SSE handler: updates current values */
    window.onReadingEvent = function (r) {
        var val = Number(r[field]);
        var formatted = formatValue(val);

        document.getElementById('sensor-current').textContent = formatted;

        var avgField = 'avg_' + field;
        if (r[avgField] !== undefined) {
            document.getElementById('sensor-avg').textContent = formatValue(Number(r[avgField]));
        }

        if (currentPeriod !== 'day') return;

        /* prepend to table */
        var st = statusFor(val);
        var dateStr = formatTime(r.timestamp, 'day');
        var tr = document.createElement('tr');
        tr.innerHTML =
            '<td>' + escapeHtml(dateStr) + '</td>' +
            '<td class="val-cell">' + escapeHtml(formatted + ' ' + units) + '</td>' +
            '<td class="val-cell">' + escapeHtml(formatted + ' ' + units) + '</td>' +
            '<td class="val-cell">' + escapeHtml(formatted + ' ' + units) + '</td>' +
            '<td class="val-cell">1</td>' +
            '<td><span class="status-badge ' + st.cls + '">' + st.label + '</span></td>';
        if (tableBody.firstChild) {
            tableBody.insertBefore(tr, tableBody.firstChild);
        } else {
            tableBody.appendChild(tr);
        }
        while (tableBody.children.length > MAX_TABLE_ROWS) {
            tableBody.removeChild(tableBody.lastChild);
        }
    };
});