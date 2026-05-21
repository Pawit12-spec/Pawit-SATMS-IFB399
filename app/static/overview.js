(function () {
    /* ---------- Initial site data injected by template ---------- */
    var SITES_DATA = window.OVERVIEW_SITES_DATA || [];

    var STATUS_CFG = {
        escalated: { label: 'ESCALATED', color: '#b00020' },
        critical:  { label: 'CRITICAL',  color: '#dc5858' },
        warning:   { label: 'WARNING',   color: '#e8960a' },
        normal:    { label: 'NORMAL',    color: '#009390' },
    };

    var STATUS_CFG_CB = {
        critical: { label: 'CRITICAL', color: '#D55E00' },
        warning: { label: 'WARNING', color: '#E69F00' },
        normal: { label: 'NORMAL', color: '#0072B2' },
    };

    var COLOR_RANGES = {
        temperature: [
            { min: 0, max: 28, color: '#00ff08' },
            { min: 29, max: 33, color: '#ffd800' },
            { min: 34, max: Infinity, color: '#dc5858' }
        ],
        humidity: [
            { min: 0, max: 57, color: '#00ff08' },
            { min: 58, max: 62, color: '#ffd800' },
            { min: 63, max: Infinity, color: '#dc5858' }
        ],
        co2: [
            { min: 0, max: 920, color: '#00ff08' },
            { min: 921, max: 1000, color: '#ffd800' },
            { min: 1001, max: Infinity, color: '#dc5858' }
        ],
        aqi: [
            { min: 0, max: 27, color: '#00ff08' },
            { min: 28, max: 31, color: '#ffd800' },
            { min: 32, max: Infinity, color: '#dc5858' }
        ]
    };

    var COLOR_RANGES_CB = {
        temperature: [
            { min: 0, max: 28, color: '#009E73' },
            { min: 29, max: 33, color: '#E69F00' },
            { min: 34, max: Infinity, color: '#D55E00' }
        ],
        humidity: [
            { min: 0, max: 57, color: '#009E73' },
            { min: 58, max: 62, color: '#E69F00' },
            { min: 63, max: Infinity, color: '#D55E00' }
        ],
        co2: [
            { min: 0, max: 920, color: '#009E73' },
            { min: 921, max: 1000, color: '#E69F00' },
            { min: 1001, max: Infinity, color: '#D55E00' }
        ],
        aqi: [
            { min: 0, max: 27, color: '#009E73' },
            { min: 28, max: 31, color: '#E69F00' },
            { min: 32, max: Infinity, color: '#D55E00' }
        ]
    };

    function isColorblind() {
        return document.documentElement.getAttribute('data-colorblind') === 'on';
    }

    function getColorForValue(value, rangeType) {
        if (value == null) return '#666';
        var ranges = (isColorblind() ? COLOR_RANGES_CB : COLOR_RANGES)[rangeType];
        for (var i = 0; i < ranges.length; i++) {
            if (value >= ranges[i].min && value <= ranges[i].max) return ranges[i].color;
        }
        return isColorblind() ? '#E69F00' : '#e8960a';
    }

    function setInner(id, html) { var el = document.getElementById(id); if (el) el.innerHTML = html; }
    function setText(id, text) { var el = document.getElementById(id); if (el) el.textContent = text; }

    function findSiteData(siteId) {
        for (var i = 0; i < SITES_DATA.length; i++) {
            if (SITES_DATA[i].site_id === siteId) return SITES_DATA[i];
        }
        return null;
    }

    function escapeHtml(str) {
        var div = document.createElement('div');
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }

    /* ---------- Suppression badges on initial load ---------- */
    SITES_DATA.forEach(function (site) {
        if (site.suppressed) {
            document.querySelectorAll('.site-card[data-site-id="' + site.site_id + '"]').forEach(function (card) {
                if (!card.querySelector('[data-review]')) {
                    var badge = document.createElement('span');
                    badge.className = 'site-status-badge badge-normal';
                    badge.setAttribute('data-review', '1');
                    badge.textContent = 'REVIEWED';
                    card.querySelector('.site-card-footer').appendChild(badge);
                }
            });
        }
    });

    /* ---------- Search ---------- */
    var searchInput = document.getElementById('site-search');
    if (searchInput) {
        searchInput.addEventListener('input', function () {
            var q = this.value.toLowerCase();
            document.querySelectorAll('#all-cards .site-card').forEach(function (card) {
                var name = (card.dataset.name || '').toLowerCase();
                var region = (card.dataset.region || '').toLowerCase();
                card.style.display = (name.includes(q) || region.includes(q)) ? '' : 'none';
            });
        });
    }

    /* ---------- Modal refs ---------- */
    var backdrop = document.getElementById('site-modal-backdrop');
    var overlay = document.getElementById('site-modal-overlay');
    var closeBtn = document.getElementById('site-modal-close');
    var closeBtnB = document.getElementById('modal-btn-close-bottom');
    var ackBtn = document.getElementById('modal-btn-acknowledge');
    var viewBtn = document.getElementById('modal-btn-view');
    var logsBtn = document.getElementById('modal-btn-logs');
    var cancelSuppressionBtn = document.getElementById('modal-btn-cancel-suppression');
    var actionsDefault = document.getElementById('modal-actions-default');
    var reviewPanel = document.getElementById('review-form-panel');
    var reviewSubmit = document.getElementById('review-submit-btn');
    var reviewCancel = document.getElementById('review-cancel-btn');
    var logPanel = document.getElementById('review-log-panel');
    var logList = document.getElementById('review-log-list');
    var logBackBtn = document.getElementById('review-log-back');

    var currentSiteId = null;
    var currentStatus = 'normal';

    /* ---------- Modal sensor population ---------- */
    function updateModalSensorValues(siteId) {
        var siteData = findSiteData(siteId) || {};
        var temp = siteData.temperature_c;
        var hum = siteData.humidity_pct;
        var co2 = siteData.co2_ppm;
        var aqi = siteData.aqi;
        var status = siteData.status || 'normal';
        var _cfg = isColorblind() ? STATUS_CFG_CB : STATUS_CFG;
        var cfg = _cfg[status] || _cfg.normal;

        if (temp != null) {
            var tc = getColorForValue(temp, 'temperature');
            setInner('modal-temp-current', '<span style="color:' + tc + ';">' + Math.round(temp) + '</span><span class="modal-sensor-unit" style="color:' + tc + ';">°C</span>');
        } else {
            setInner('modal-temp-current', '--<span class="modal-sensor-unit">°C</span>');
        }
        setText('modal-temp-avg', 'AVG ' + (siteData.avg_temperature_c != null ? Math.round(siteData.avg_temperature_c) + '°C' : '--°C'));

        if (hum != null) {
            var hc = getColorForValue(hum, 'humidity');
            setInner('modal-hum-current', '<span style="color:' + hc + ';">' + Math.round(hum) + '</span><span class="modal-sensor-unit" style="color:' + hc + ';">%</span>');
        } else {
            setInner('modal-hum-current', '--<span class="modal-sensor-unit">%</span>');
        }
        setText('modal-hum-avg', 'AVG ' + (siteData.avg_humidity_pct != null ? Math.round(siteData.avg_humidity_pct) + '%' : '--%'));

        if (co2 != null) {
            var cc = getColorForValue(co2, 'co2');
            setInner('modal-co2-current', '<span style="color:' + cc + ';">' + Math.round(co2) + '</span><span class="modal-sensor-unit" style="color:' + cc + ';">ppm</span>');
        } else {
            setInner('modal-co2-current', '--<span class="modal-sensor-unit">ppm</span>');
        }
        setText('modal-co2-avg', 'AVG ' + (siteData.avg_co2_ppm != null ? Math.round(siteData.avg_co2_ppm) + ' ppm' : '-- ppm'));

        if (aqi != null) {
            var ac = getColorForValue(aqi, 'aqi');
            setInner('modal-aqi-current', '<span style="color:' + ac + ';">' + aqi.toFixed(1) + '</span><span class="modal-sensor-unit" style="color:' + ac + ';">µg/m³</span>');
        } else {
            setInner('modal-aqi-current', '--<span class="modal-sensor-unit">µg/m³</span>');
        }
        setText('modal-aqi-avg', 'AVG ' + (siteData.avg_aqi  != null ? siteData.avg_aqi .toFixed(1) + ' µg/m³' : '-- µg/m³'));

        var dot = document.getElementById('modal-status-dot');
        var text = document.getElementById('modal-status-text');
        if (dot) dot.style.background = cfg.color;
        if (text) { text.textContent = cfg.label; text.style.color = cfg.color; }

        document.getElementById('modal-accent').style.background = cfg.color;
        currentStatus = status;

        var occWrapper = document.getElementById('modal-suppressed-occurrence-wrapper');
        var occCount   = document.getElementById('modal-suppressed-occurrence-count');
        var count      = siteData.suppressed_occurrence_count || 0;
        if (occWrapper) {
            occWrapper.style.display = (siteData.suppressed && count > 0) ? '' : 'none';
        }
        if (occCount) occCount.textContent = count;

        if (ackBtn)   ackBtn.style.display   = (status === 'normal') ? 'none' : '';
        if (closeBtnB) closeBtnB.style.display = (status === 'normal') ? '' : 'none';

        var danalysis = document.getElementById('modal-analysis');
        var danalysismsg = document.getElementById('modal-analysis-msg');
        if (danalysis && danalysismsg) {
            var msgs = siteData.analysis || [];
            danalysis.style.display = msgs.length > 0 ? '' : 'none';
            danalysismsg.textContent = msgs.length > 0 ? msgs[0] : '';
        }

    }

    /* ---------- Modal open / close ---------- */
    function openModal(card) {
        currentSiteId = card.dataset.siteId;
        setText('modal-site-id', currentSiteId.substring(0, 8));
        setText('modal-site-name', card.dataset.name || 'Unknown Site');
        var region = card.dataset.region || '';
        var address = card.dataset.address || 'Substation Room';
        setText('modal-site-location', (region ? region + ' · ' : '') + address);
        updateModalSensorValues(currentSiteId);
        showDefaultActions();
        backdrop.setAttribute('aria-hidden', 'false');
        overlay.setAttribute('aria-hidden', 'false');
        document.body.classList.add('modal-open');
    }

    function closeModal() {
        backdrop.setAttribute('aria-hidden', 'true');
        overlay.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('modal-open');
        showDefaultActions();
    }

    function showDefaultActions() {
        if (actionsDefault) actionsDefault.style.display = '';
        if (reviewPanel) reviewPanel.style.display = 'none';
        if (logPanel) logPanel.style.display = 'none';
    }

    function updateReviewSubmitLabel() {
        if (!reviewSubmit) return;
        var t = document.getElementById('review-timeout');
        reviewSubmit.textContent = (t && parseInt(t.value, 10) > 0) ? 'SUPPRESS & LOG' : 'LOG REVIEW';
    }

    function showReviewForm() {
        if (actionsDefault) actionsDefault.style.display = 'none';
        if (reviewPanel) reviewPanel.style.display = 'block';
        if (logPanel) logPanel.style.display = 'none';
        document.getElementById('review-severity').value = 'medium';
        document.getElementById('review-timeout').value = '60';
        document.getElementById('review-comment').value = '';
        updateReviewSubmitLabel();
        var timeoutEl = document.getElementById('review-timeout');
        if (timeoutEl) timeoutEl.addEventListener('change', updateReviewSubmitLabel);
    }

    function showReviewLog() {
        if (actionsDefault) actionsDefault.style.display = 'none';
        if (reviewPanel) reviewPanel.style.display = 'none';
        if (logPanel) logPanel.style.display = 'block';
        if (cancelSuppressionBtn) {
            var siteData = findSiteData(currentSiteId) || {};
            cancelSuppressionBtn.style.display = siteData.suppressed ? 'inline-block' : 'none';
        }
        loadReviewLog(currentSiteId);
    }

    /* ---------- Attach card click handlers ---------- */
    function attachCardHandlers(container) {
        container.querySelectorAll('.site-card').forEach(function (card) {
            card.addEventListener('click', function () { openModal(card); });
        });
    }

    document.querySelectorAll('.site-card').forEach(function (card) {
        card.addEventListener('click', function () { openModal(card); });
    });

    if (closeBtn) closeBtn.addEventListener('click', closeModal);
    if (closeBtnB) closeBtnB.addEventListener('click', closeModal);
    if (backdrop) backdrop.addEventListener('click', closeModal);
    if (ackBtn) ackBtn.addEventListener('click', showReviewForm);
    if (logsBtn) logsBtn.addEventListener('click', showReviewLog);
    if (reviewCancel) reviewCancel.addEventListener('click', showDefaultActions);
    if (logBackBtn) logBackBtn.addEventListener('click', showDefaultActions);

    /* ---------- Cancel suppression ---------- */
    if (cancelSuppressionBtn) {
        cancelSuppressionBtn.addEventListener('click', function () {
            if (!currentSiteId) return;
            cancelSuppressionBtn.disabled = true;
            cancelSuppressionBtn.textContent = 'CANCELLING…';

            fetch('/api/reviews/' + encodeURIComponent(currentSiteId) + '/cancel', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            })
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    cancelSuppressionBtn.disabled = false;
                    cancelSuppressionBtn.textContent = 'CANCEL SUPPRESSION';
                    if (data.success) {
                        var site = findSiteData(currentSiteId);
                        if (site) site.suppressed = false;
                        document.querySelectorAll('.site-card[data-site-id="' + currentSiteId + '"] [data-review]').forEach(function (b) { b.remove(); });
                        cancelSuppressionBtn.style.display = 'none';
                        rebuildStatusSections();
                        loadReviewLog(currentSiteId);
                    } else {
                        alert('Failed to cancel suppression: ' + (data.error || 'Unknown error'));
                    }
                })
                .catch(function (err) {
                    cancelSuppressionBtn.disabled = false;
                    cancelSuppressionBtn.textContent = 'CANCEL SUPPRESSION';
                    alert('Network error: ' + err.message);
                });
        });
    }

    /* ---------- Submit review ---------- */
    if (reviewSubmit) {
        reviewSubmit.addEventListener('click', function () {
            var severity = document.getElementById('review-severity').value;
            var timeout = document.getElementById('review-timeout').value;
            var comment = document.getElementById('review-comment').value.trim();

            reviewSubmit.disabled = true;
            reviewSubmit.textContent = 'SUBMITTING…';

            fetch('/api/reviews', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    site_id: currentSiteId,
                    severity: severity,
                    timeout_minutes: parseInt(timeout, 10),
                    comment: comment,
                    status_at_review: currentStatus
                })
            })
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    reviewSubmit.disabled = false;
                    updateReviewSubmitLabel();
                    if (data.success) {
                        var suppressing = parseInt(document.getElementById('review-timeout').value, 10) > 0;
                        if (suppressing) {
                            document.querySelectorAll('.site-card[data-site-id="' + currentSiteId + '"]').forEach(function (card) {
                                if (!card.querySelector('.badge-normal[data-review]')) {
                                    var badge = document.createElement('span');
                                    badge.className = 'site-status-badge badge-normal';
                                    badge.setAttribute('data-review', '1');
                                    badge.textContent = 'REVIEWED';
                                    card.querySelector('.site-card-footer').appendChild(badge);
                                }
                            });
                            var site = findSiteData(currentSiteId);
                            if (site) site.suppressed = true;
                        }
                        closeModal();
                    } else {
                        alert('Failed to submit review: ' + (data.error || 'Unknown error'));
                    }
                })
                .catch(function (err) {
                    reviewSubmit.disabled = false;
                    updateReviewSubmitLabel();
                    alert('Network error: ' + err.message);
                });
        });
    }

    /* ---------- Review log ---------- */
    function loadReviewLog(siteId) {
        if (!siteId) return;
        logList.innerHTML = '<div class="empty-section-msg">Loading…</div>';

        fetch('/api/reviews/' + encodeURIComponent(siteId))
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (!data.reviews || data.reviews.length === 0) {
                    logList.innerHTML = '<div class="empty-section-msg">No reviews recorded for this site.</div>';
                    return;
                }
                logList.innerHTML = data.reviews.map(function (r) {
                    var dt = new Date(r.reviewed_at);
                    var timeStr = dt.toLocaleDateString() + ' ' + dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

                    if (r.status_at_review === 'suppression_cancelled') {
                        return '<div class="modal-sensor-card" style="gap:6px;">'
                            + '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">'
                            + '<span class="site-card-name" style="font-size:14px;">' + escapeHtml(r.reviewed_by) + '</span>'
                            + '<span class="site-card-time">' + escapeHtml(timeStr) + '</span>'
                            + '</div>'
                            + '<div style="display:flex;align-items:center;gap:8px;">'
                            + '<span class="site-status-badge badge-critical">SUPPRESSION CANCELLED</span>'
                            + '</div></div>';
                    }

                    var sevMap = { low: 'badge-normal', medium: 'badge-warning', high: 'badge-warning', critical: 'badge-critical' };
                    var statusMap = { normal: 'badge-normal', warning: 'badge-warning', critical: 'badge-critical' };
                    var html = '<div class="modal-sensor-card" style="gap:6px;">'
                        + '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">'
                        + '<span class="site-card-name" style="font-size:14px;">' + escapeHtml(r.reviewed_by) + '</span>'
                        + '<span class="site-card-time">' + escapeHtml(timeStr) + '</span>'
                        + '</div>'
                        + '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">'
                        + '<span class="site-status-badge ' + (statusMap[r.status_at_review] || 'badge-normal') + '">' + escapeHtml(r.status_at_review.toUpperCase()) + '</span>'
                        + '<span class="site-status-badge ' + (sevMap[r.severity] || 'badge-warning') + '">' + escapeHtml((r.severity || 'medium').toUpperCase()) + '</span>';
                    if (r.timeout_minutes > 0) {
                        html += '<span class="site-status-badge badge-normal">SUPPRESSED ' + r.timeout_minutes + ' MIN</span>';
                    }
                    html += '</div>';
                    if (r.comment) {
                        html += '<div class="modal-sensor-avg" style="font-size:13px;margin-top:4px;">' + escapeHtml(r.comment) + '</div>';
                    }
                    html += '</div>';
                    return html;
                }).join('');
            })
            .catch(function () {
                logList.innerHTML = '<div class="empty-section-msg">Failed to load reviews.</div>';
            });
    }

    /* ---------- View full room ---------- */
    if (viewBtn) {
        viewBtn.addEventListener('click', function () {
            window.location.href = currentSiteId
                ? '/dashboard/' + encodeURIComponent(currentSiteId)
                : '/dashboard';
        });
    }

    /* ---------- Build card HTML (for dynamic sections) ---------- */
    function buildCardHtml(site, status) {
        var region = (site.region || site.address || '').toUpperCase();
        return '<div class="site-card ' + status + '-card"'
            + ' data-site-id="' + escapeHtml(site.site_id) + '"'
            + ' data-name="' + escapeHtml(site.name || '') + '"'
            + ' data-region="' + escapeHtml(site.region || '') + '"'
            + ' data-address="' + escapeHtml(site.address || 'Substation Room') + '"'
            + ' data-status="' + escapeHtml(status) + '"'
            + ' data-last-reading="' + escapeHtml(site.last_reading || '--') + '">'
            + '<div class="site-card-id">' + escapeHtml(site.site_id.substring(0, 8)) + '</div>'
            + '<div class="site-card-name">' + escapeHtml(site.name || '') + '</div>'
            + '<div class="site-card-region">' + escapeHtml(region) + '</div>'
            + '<div class="site-card-footer">'
            + '<span class="site-status-badge badge-' + status + '">' + status.toUpperCase() + '</span>'
            + '<span class="site-card-time">' + escapeHtml(site.last_reading || '--') + '</span>'
            + '</div></div>';
    }

    /* ---------- Rebuild critical/warning sections ---------- */
    function rebuildStatusSections() {
        var escalatedContainer = document.getElementById('escalated-cards');
        var criticalContainer  = document.getElementById('critical-cards');
        var warningContainer   = document.getElementById('warning-cards');

        var escalated = SITES_DATA.filter(function (s) { return s.escalated && !s.suppressed; });
        var criticals = SITES_DATA.filter(function (s) { return s.status === 'critical' && !s.escalated && !s.suppressed; });
        var warnings  = SITES_DATA.filter(function (s) { return s.status === 'warning'  && !s.escalated && !s.suppressed; });

        if (escalatedContainer) {
            escalatedContainer.innerHTML = escalated.length
                ? escalated.map(function (s) { return buildCardHtml(s, 'escalated'); }).join('')
                : '<div class="empty-section-msg">No escalated alerts</div>';
            attachCardHandlers(escalatedContainer);
        }

        criticalContainer.innerHTML = criticals.length
            ? criticals.map(function (s) { return buildCardHtml(s, 'critical'); }).join('')
            : '<div class="empty-section-msg">No critical sites</div>';

        warningContainer.innerHTML = warnings.length
            ? warnings.map(function (s) { return buildCardHtml(s, 'warning'); }).join('')
            : '<div class="empty-section-msg">No warnings</div>';

        attachCardHandlers(criticalContainer);
        attachCardHandlers(warningContainer);
    }

    /* ---------- SSE live updates ---------- */
    window.onReadingEvent = function (r) {
        if (!r.site_id) return;
        var site = findSiteData(r.site_id);
        if (!site) return;

        site.temperature_c      = r.temperature_c;
        site.humidity_pct       = r.humidity_pct;
        site.co2_ppm            = r.co2_ppm;
        site.aqi                = r.aqi;
        site.status             = r.status || 'normal';
        site.last_reading       = 'JUST NOW';
        site.analysis           = r.analysis || [];
        site.avg_temperature_c  = r.avg_temperature_c;
        site.avg_humidity_pct   = r.avg_humidity_pct;
        site.avg_co2_ppm        = r.avg_co2_ppm;
        site.avg_aqi            = r.avg_aqi;


        var status = site.status;
        document.querySelectorAll('.site-card[data-site-id="' + r.site_id + '"]').forEach(function (card) {
            card.dataset.status = status;
            card.dataset.lastReading = 'JUST NOW';
            card.classList.remove('critical-card', 'warning-card', 'normal-card', 'escalated-card');
            card.classList.add(status + '-card');
            var badge = card.querySelector('.site-status-badge');
            if (badge) { badge.className = 'site-status-badge badge-' + status; badge.textContent = status.toUpperCase(); }
            var timeEl = card.querySelector('.site-card-time');
            if (timeEl) timeEl.textContent = 'JUST NOW';
        });

        if (currentSiteId === r.site_id && overlay.getAttribute('aria-hidden') === 'false') {
            updateModalSensorValues(r.site_id);
        }

        rebuildStatusSections();
    };

    /* ---------- Periodic refresh from API ---------- */
    function refreshSites() {
        fetch('/api/sites')
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (!data.sites) return;
                SITES_DATA = data.sites;

                data.sites.forEach(function (site) {
                    var status = site.status || 'normal';
                    document.querySelectorAll('#all-cards .site-card[data-site-id="' + site.site_id + '"]').forEach(function (card) {
                        card.dataset.status = status;
                        card.dataset.lastReading = site.last_reading || '--';
                        card.classList.remove('critical-card', 'warning-card', 'normal-card', 'escalated-card');
                        card.classList.add(status + '-card');
                        var badge = card.querySelector('.site-status-badge');
                        if (badge) { badge.className = 'site-status-badge badge-' + status; badge.textContent = status.toUpperCase(); }
                        var timeEl = card.querySelector('.site-card-time');
                        if (timeEl) timeEl.textContent = site.last_reading || '--';
                        var reviewBadge = card.querySelector('[data-review]');
                        if (site.suppressed && !reviewBadge) {
                            var rb = document.createElement('span');
                            rb.className = 'site-status-badge badge-normal';
                            rb.setAttribute('data-review', '1');
                            rb.textContent = 'REVIEWED';
                            card.querySelector('.site-card-footer').appendChild(rb);
                        } else if (!site.suppressed && reviewBadge) {
                            reviewBadge.remove();
                        }
                    });
                });

                rebuildStatusSections();
            })
            .catch(function (err) { console.warn('Failed to refresh sites:', err); });
    }

    setInterval(refreshSites, 10000);

})();
