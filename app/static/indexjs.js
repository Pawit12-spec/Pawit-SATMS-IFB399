
function LineChartMaker(canvas, color) {
    color = color || 'rgba(59,130,246,1)';
    var gridColor = 'rgba(160,160,160,0.12)';
    var tickColor = 'rgba(160,160,160,0.7)';

    return new Chart(canvas, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                data: [],
                borderWidth: 2,
                tension: 0.4,
                fill: false,
                borderColor: color,
                pointRadius: 0,
                pointHoverRadius: 4,
                pointHoverBackgroundColor: color,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: {
                duration: 400,
                easing: 'easeInOutQuart'
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    mode: 'index',
                    intersect: false,
                }
            },
            scales: {
                x: {
                    display: true,
                    ticks: {
                        color: tickColor,
                        font: { size: 10 },
                        maxTicksLimit: 5,
                        maxRotation: 0,
                    },
                    grid: { color: gridColor },
                    border: { display: false },
                },
                y: {
                    display: true,
                    ticks: {
                        color: tickColor,
                        font: { size: 10 },
                        maxTicksLimit: 4,
                    },
                    grid: { color: gridColor },
                    border: { display: false },
                }
            }
        }
    });
}

function DataToChart(chart, time, sensor, storageKey) {
    chart.data.labels.push(time);
    chart.data.datasets[0].data.push(sensor);

    if (chart.data.labels.length > 20) {
        chart.data.labels.shift();
        chart.data.datasets[0].data.shift();
    }

    if (storageKey) {
        try {
            sessionStorage.setItem(storageKey, JSON.stringify({
                labels: chart.data.labels,
                data: chart.data.datasets[0].data
            }));
        } catch (e) {}
    }

    chart.update();
};

function RestoreChart(chart, storageKey) {
    try {
        var saved = sessionStorage.getItem(storageKey);
        if (!saved) return;
        var parsed = JSON.parse(saved);
        if (parsed && parsed.labels && parsed.data) {
            chart.data.labels = parsed.labels;
            chart.data.datasets[0].data = parsed.data;
            chart.update('none');
        }
    } catch (e) {}
};

// cardIds: array of card element IDs e.g. ['card-temp', 'card-hum', ...]
// valueSpanIds: array of span IDs whose textContent should be saved e.g. ['temp-current', 'temp-avg', ...]
function SaveDashboardState(cardIds, valueSpanIds) {
    try {
        var cards = {};
        cardIds.forEach(function (id) {
            var el = document.getElementById(id);
            if (!el) return;
            var dot = el.querySelector('.card-dot');
            var status = el.querySelector('.card-status');
            cards[id] = {
                anomaly: el.classList.contains('anomaly'),
                dotClass: dot ? dot.className : '',
                statusText: status ? status.textContent : '',
                statusAnomaly: status ? status.classList.contains('anomaly') : false,
            };
        });
        var values = {};
        valueSpanIds.forEach(function (id) {
            var el = document.getElementById(id);
            if (el) values[id] = el.textContent;
        });
        sessionStorage.setItem('dashboard_state', JSON.stringify({ cards: cards, values: values }));
    } catch (e) {}
};

function RestoreDashboardState() {
    try {
        var saved = sessionStorage.getItem('dashboard_state');
        if (!saved) return;
        var parsed = JSON.parse(saved);
        if (!parsed) return;
        if (parsed.cards) {
            Object.keys(parsed.cards).forEach(function (id) {
                var el = document.getElementById(id);
                if (!el) return;
                var s = parsed.cards[id];
                var dot = el.querySelector('.card-dot');
                var status = el.querySelector('.card-status');
                if (s.anomaly) { el.classList.add('anomaly'); } else { el.classList.remove('anomaly'); }
                if (dot) dot.className = s.dotClass;
                if (status) {
                    status.textContent = s.statusText;
                    if (s.statusAnomaly) { status.classList.add('anomaly'); } else { status.classList.remove('anomaly'); }
                }
            });
        }
        if (parsed.values) {
            Object.keys(parsed.values).forEach(function (id) {
                var el = document.getElementById(id);
                if (el) el.textContent = parsed.values[id];
            });
        }
    } catch (e) {}
}; 