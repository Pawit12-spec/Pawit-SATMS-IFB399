(function () {
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    var icon = document.getElementById('theme-icon');
    if (icon) icon.textContent = theme === 'dark' ? '🌙' : '☀️';
    localStorage.setItem('theme', theme);
  }

  function applyColorblind(mode) {
    document.documentElement.setAttribute('data-colorblind', mode);
    localStorage.setItem('colorblind', mode);
  }

  applyTheme(document.documentElement.getAttribute('data-theme') || 'light');
  applyColorblind(document.documentElement.getAttribute('data-colorblind') || 'off');

  document.getElementById('theme-toggle').addEventListener('click', function () {
    var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    fetch('/api/preferences', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preferences: { theme: next } })
    }).catch(function () { });
  });

  var menu = document.getElementById('user-menu');
  var toggle = document.getElementById('user-menu-toggle');
  if (toggle) {
    toggle.addEventListener('click', function (e) {
      e.stopPropagation();
      menu.classList.toggle('open');
    });
    document.addEventListener('click', function () {
      menu.classList.remove('open');
    });
  }

  var lastReading = null;
  var es = new EventSource(window.EVENTS_URL);

  es.addEventListener('reading', function (evt) {
    var r = JSON.parse(evt.data);
    lastReading = new Date(r.timestamp);
    if (window.onReadingEvent) window.onReadingEvent(r);
    updateLatency();
  });

  function updateLatency() {
    if (!lastReading) return;
    var secs = Math.floor((Date.now() - lastReading.getTime()) / 1000);
    var text;
    if (secs < 60) text = secs + 's';
    else if (secs < 3600) text = Math.floor(secs / 60) + 'm ' + (secs % 60) + 's';
    else text = Math.floor(secs / 3600) + 'h ' + Math.floor((secs % 3600) / 60) + 'm';

    var el = document.getElementById('latency-text');
    if (el) el.textContent = text;

    var threshold = parseInt((window.USER_PREFERENCES || {}).data_delay_threshold || 120, 10);
    var banner = document.getElementById('stale-banner');
    var indicator = document.getElementById('latency-indicator');
    if (secs > threshold) {
      if (banner) banner.style.display = 'flex';
      var st = document.getElementById('stale-time');
      if (st) st.textContent = text;
      if (indicator) indicator.classList.add('stale');
    } else {
      if (banner) banner.style.display = 'none';
      if (indicator) indicator.classList.remove('stale');
    }
  }

  setInterval(updateLatency, 1000);

  var modal = document.getElementById('profile-settings-modal');
  var openBtn = document.getElementById('profile-settings-btn');
  var closeBtn = document.getElementById('pref-modal-close');
  var saveBtn = document.getElementById('pref-save-btn');
  var status = document.getElementById('pref-save-status');

  var nameInput = document.getElementById('pref-name');
  var themeSelect = document.getElementById('pref-theme');
  var pageSelect = document.getElementById('pref-default-page');
  var delaySelect = document.getElementById('pref-delay');
  var cbSelect = document.getElementById('pref-colorblind');

  var originalName = '';

  function openModal() {
    var prefs = window.USER_PREFERENCES || {};
    var nameEl = document.querySelector('.user-menu-name');
    originalName = nameEl ? nameEl.textContent.trim() : '';
    nameInput.value = originalName;
    themeSelect.value = prefs.theme || document.documentElement.getAttribute('data-theme') || 'light';
    pageSelect.value = prefs.default_page || 'index';
    delaySelect.value = String(prefs.data_delay_threshold || 120);
    cbSelect.value = document.documentElement.getAttribute('data-colorblind') || 'off';
    status.textContent = '';
    status.className = 'pref-save-status';
    modal.style.display = 'flex';
    document.getElementById('user-menu').classList.remove('open');
  }

  function closeModal() {
    modal.style.display = 'none';
  }

  if (openBtn) openBtn.addEventListener('click', openModal);
  if (closeBtn) closeBtn.addEventListener('click', closeModal);

  if (cbSelect) {
    cbSelect.addEventListener('change', function () {
      var mode = cbSelect.value;
      applyColorblind(mode);
      window.USER_PREFERENCES = Object.assign(window.USER_PREFERENCES || {}, { colorblind: mode });
      fetch('/api/preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preferences: { colorblind: mode } })
      }).catch(function () { });
    });
  }

  modal.addEventListener('click', function (e) {
    if (e.target === modal) closeModal();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal.style.display === 'flex') closeModal();
  });

  saveBtn.addEventListener('click', function () {
    var newName = nameInput.value.trim();
    if (!newName) {
      status.textContent = 'Name cannot be empty';
      status.className = 'pref-save-status pref-save-err';
      return;
    }

    var prefs = {
      theme: themeSelect.value,
      default_page: pageSelect.value,
      data_delay_threshold: parseInt(delaySelect.value, 10),
      colorblind: cbSelect.value,
    };
    var newColorblind = cbSelect.value;

    saveBtn.disabled = true;
    status.textContent = '';

    var calls = [
      fetch('/api/preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preferences: prefs }),
      }).then(function (r) { return r.json(); })
    ];

    if (newName !== originalName) {
      calls.push(
        fetch('/api/profile', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ full_name: newName }),
        }).then(function (r) { return r.json(); })
      );
    }

    Promise.all(calls)
      .then(function (results) {
        var failed = results.find(function (d) { return !d.success; });
        if (failed) {
          status.textContent = failed.error || 'Failed to save';
          status.className = 'pref-save-status pref-save-err';
          return;
        }

        window.USER_PREFERENCES = Object.assign(window.USER_PREFERENCES || {}, prefs);
        applyTheme(prefs.theme);
        applyColorblind(newColorblind);

        if (newName !== originalName) {
          originalName = newName;
          document.querySelectorAll('.user-menu-name, .dropdown-name').forEach(function (el) {
            el.textContent = newName;
          });
          var avatar = document.querySelector('.user-menu .ellipse');
          if (avatar) avatar.textContent = newName.charAt(0).toUpperCase();
        }

        status.textContent = 'Saved';
        status.className = 'pref-save-status pref-save-ok';
        setTimeout(closeModal, 800);
      })
      .catch(function () {
        status.textContent = 'Failed to save';
        status.className = 'pref-save-status pref-save-err';
      })
      .finally(function () {
        saveBtn.disabled = false;
      });
  });

  (function () {
    var btn = document.getElementById('sidebar-collapse-toggle');
    if (!btn) return;
    var html = document.documentElement;

    function applyState(collapsed) {
      if (collapsed) {
        html.setAttribute('data-sidebar', 'collapsed');
        btn.setAttribute('aria-expanded', 'false');
        btn.setAttribute('aria-label', 'Expand sidebar');
        btn.title = 'Expand sidebar';
      } else {
        html.removeAttribute('data-sidebar');
        btn.setAttribute('aria-expanded', 'true');
        btn.setAttribute('aria-label', 'Collapse sidebar');
        btn.title = 'Collapse sidebar';
      }
    }

    applyState(html.getAttribute('data-sidebar') === 'collapsed');

    btn.addEventListener('click', function () {
      var willCollapse = html.getAttribute('data-sidebar') !== 'collapsed';
      applyState(willCollapse);
      try { localStorage.setItem('sidebarCollapsed', willCollapse ? '1' : '0'); } catch (e) { /* noop */ }
    });
  })();
})();

/* ---- Shared CSV Export ---- */
function initCsvExport(baseUrl, filename) {
  var btn = document.getElementById('exportCsvBtn');
  var start = document.getElementById('exportStart');
  var end = document.getElementById('exportEnd');
  if (!btn) return;

  var now = new Date();
  var yesterday = new Date(now.getTime() - 86400000);
  end.value = now.toISOString().slice(0, 16);
  start.value = yesterday.toISOString().slice(0, 16);

  btn.addEventListener('click', function () {
    if (!start.value || !end.value) { alert('Please select both a start and end date.'); return; }
    if (new Date(end.value) <= new Date(start.value)) { alert('End date must be after start date.'); return; }

    var sep = baseUrl.indexOf('?') === -1 ? '?' : '&';
    var url = baseUrl + sep
      + 'start=' + encodeURIComponent(new Date(start.value).toISOString())
      + '&end=' + encodeURIComponent(new Date(end.value).toISOString());

    btn.disabled = true;
    btn.textContent = 'Exporting...';
    fetch(url)
      .then(function (res) {
        if (!res.ok) return res.json().then(function (b) { throw new Error(b.error || 'Export failed'); });
        return res.blob();
      })
      .then(function (blob) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(a.href);
      })
      .catch(function (err) { alert(err.message || 'Export failed'); })
      .finally(function () { btn.disabled = false; btn.textContent = 'Export to CSV'; });
  });
}

/* ---- Site Switcher ---- */
function initSiteSwitcher(selectEl, currentSiteId, basePath) {
  if (!selectEl) return;

  fetch('/api/sites')
    .then(function (res) { return res.json(); })
    .then(function (data) {
      (data.sites || []).forEach(function (site) {
        var opt = document.createElement('option');
        opt.value = site.site_id;
        opt.textContent = site.name;
        if (site.site_id === currentSiteId) opt.selected = true;
        selectEl.appendChild(opt);
      });
    });

  selectEl.addEventListener('change', function () {
    window.location.href = basePath + '/' + this.value;
  });
}
