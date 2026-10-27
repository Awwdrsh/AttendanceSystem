/*
 * Hajiri page behaviour: toasts, confirmations, modals, command palette, theme, offline banner, chart tooltips and the
 * Alpine components used by the shared template components (date picker, employee picker, file upload, camera,
 * recovery codes). Loaded with `defer` before Alpine so the components are registered on `alpine:init`.
 */
(function () {
  'use strict';
  var H = (window.Hajiri = window.Hajiri || {});
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };
  H.esc = esc;

  /* ------------------------------------------------------------ toasts (lib/toast.ts) */
  var TIMEOUT = { ok: 4000, info: 5000, warn: 6000, err: 8000 };
  var TONE = { ok: 'ok', info: 'info', warn: 'warn', err: 'bad' };
  var ICON = { ok: 'circle-check', info: 'info-circle', warn: 'alert-triangle', err: 'alert-circle' };
  H.toast = function (message, tone, title) {
    tone = tone || 'ok';
    var box = $('#hj-toasts');
    if (!box) return;
    var el = document.createElement('div');
    el.className = 'hj-toast hj-tone-' + (TONE[tone] || 'idle');
    el.setAttribute('role', tone === 'err' ? 'alert' : 'status');
    el.innerHTML = '<i class="ti ti-' + ICON[tone] + '" aria-hidden="true" style="color:var(--t-dot);font-size:18px;margin-top:1px"></i>' +
      '<div class="hj-grow">' + (title ? '<div class="hj-toast-title">' + esc(title) + '</div>' : '') +
      '<div class="hj-toast-msg"' + (title ? '' : ' style="color:var(--hj-text)"') + '>' + esc(message) + '</div></div>' +
      '<button type="button" class="hj-iconbtn" aria-label="Close"><i class="ti ti-x" aria-hidden="true"></i></button>';
    var close = function () { if (el.parentNode) el.parentNode.removeChild(el); };
    el.querySelector('button').addEventListener('click', close);
    box.appendChild(el);
    setTimeout(close, TIMEOUT[tone] || 5000);
  };
  H.toast.ok = function (m, t) { H.toast(m, 'ok', t); };
  H.toast.info = function (m, t) { H.toast(m, 'info', t); };
  H.toast.err = function (m, t) { H.toast(m, 'err', t || 'Not saved'); };

  document.body.addEventListener('hj:toast', function (e) {
    var items = (e.detail && e.detail.items) || [e.detail];
    items.forEach(function (t) { if (t && t.message) H.toast(t.message, t.tone, t.title); });
  });

  /* ------------------------------------------------------------ square JPEG (AvatarEditor.tsx squareJpeg)
   * <input type="file" data-square-jpeg="320" hx-trigger="squared" ...>: the picked photo is centre-cropped and shrunk
   * to a square JPEG in the browser, put back into the input, and only then is the HTMX request fired. */
  H.squareJpeg = function (file, size) {
    size = size || 320;
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error('That file is not a photo we can read.')); };
      img.onload = function () {
        try {
          var side = Math.min(img.naturalWidth, img.naturalHeight);
          var canvas = document.createElement('canvas');
          canvas.width = size; canvas.height = size;
          canvas.getContext('2d').drawImage(img, (img.naturalWidth - side) / 2, (img.naturalHeight - side) / 2, side, side, 0, 0, size, size);
          canvas.toBlob(function (b) {
            URL.revokeObjectURL(url);
            if (!b) { reject(new Error('The photo could not be processed.')); return; }
            resolve(new File([b], 'profile.jpg', { type: 'image/jpeg' }));
          }, 'image/jpeg', 0.85);
        } catch (err) { URL.revokeObjectURL(url); reject(new Error('The photo could not be processed.')); }
      };
      img.src = url;
    });
  };
  document.addEventListener('change', function (e) {
    var input = e.target;
    if (!input || !input.matches || !input.matches('input[type=file][data-square-jpeg]')) return;
    var f = input.files && input.files[0];
    if (!f) return;
    H.squareJpeg(f, parseInt(input.getAttribute('data-square-jpeg'), 10) || 320).then(function (out) {
      var dt = new DataTransfer();
      dt.items.add(out);
      input.files = dt.files;
      if (window.htmx) window.htmx.trigger(input, 'squared');
    }).catch(function (err) {
      input.value = '';
      H.toast(err.message, 'err', 'Photo not saved');
    });
  });

  /* ------------------------------------------------------------ HTMX wiring */
  function hasToastHeader(xhr) {
    try { return /hj:toast/.test(xhr.getResponseHeader('HX-Trigger') || ''); } catch (e) { return false; }
  }
  document.body.addEventListener('htmx:responseError', function (e) {
    var xhr = e.detail.xhr;
    if (!xhr || hasToastHeader(xhr) || xhr.getResponseHeader('HX-Redirect')) return;
    var verb = (e.detail.requestConfig && e.detail.requestConfig.verb) || 'get';
    var text = (xhr.responseText || '').trim();
    var msg = xhr.status === 404 ? 'That page or record was not found.' : xhr.status >= 500 ? 'Something went wrong on the server. Please try again.'
      : (text && text.length < 300 && text.indexOf('<') === -1 ? text : 'Request failed (' + xhr.status + ')');
    H.toast(msg, 'err', verb === 'get' ? 'Could not load' : 'Not saved');
  });
  document.body.addEventListener('htmx:sendError', function () {
    H.toast('No connection. Please check your network and try again.', 'err', 'Offline');
  });
  document.body.addEventListener('hj:close-modal', function () { $$('dialog[open]').forEach(function (d) { if (!d.classList.contains('hj-palette')) d.close(); }); });

  /* ------------------------------------------------------------ confirmation dialog (ConfirmDialog.tsx) */
  var pending = null;
  function askConfirm(opts, onYes) {
    var d = $('#hj-confirm');
    if (!d) { if (window.confirm(opts.message)) onYes(); return; }
    $('#hj-confirm-title', d).textContent = opts.title || 'Are you sure?';
    $('#hj-confirm-msg', d).textContent = opts.message;
    var ok = $('#hj-confirm-ok', d);
    ok.textContent = opts.label || 'Confirm';
    ok.className = 'hj-btn ' + (opts.tone === 'primary' ? '' : 'danger');
    pending = onYes;
    d.showModal();
    ok.focus();
  }
  H.confirm = askConfirm;
  document.addEventListener('click', function (e) {
    if (e.target.closest('#hj-confirm-ok')) { var f = pending; pending = null; $('#hj-confirm').close(); if (f) f(); }
    if (e.target.closest('#hj-confirm-cancel')) { pending = null; $('#hj-confirm').close(); }
  });
  document.body.addEventListener('htmx:confirm', function (e) {
    var elt = e.detail.elt;
    var q = e.detail.question;
    if (!q) return;
    e.preventDefault();
    askConfirm({ message: q, title: elt.getAttribute('data-confirm-title'), label: elt.getAttribute('data-confirm-label'), tone: elt.getAttribute('data-confirm-tone') },
      function () { e.detail.issueRequest(true); });
  });
  // Plain forms and links: data-confirm="message" (with the same data-confirm-* options).
  document.addEventListener('submit', function (e) {
    var f = e.target;
    var q = f.getAttribute('data-confirm');
    if (!q || f.dataset.confirmed) return;
    e.preventDefault();
    askConfirm({ message: q, title: f.getAttribute('data-confirm-title'), label: f.getAttribute('data-confirm-label'), tone: f.getAttribute('data-confirm-tone') },
      function () { f.dataset.confirmed = '1'; f.requestSubmit ? f.requestSubmit() : f.submit(); });
  }, true);

  /* ------------------------------------------------------------ modals and drawers */
  document.addEventListener('click', function (e) {
    var open = e.target.closest('[data-modal-open]');
    if (open) { var d = document.getElementById(open.getAttribute('data-modal-open')); if (d && d.showModal) { e.preventDefault(); d.showModal(); } }
    var close = e.target.closest('[data-modal-close]');
    if (close) { var dlg = close.closest('dialog'); if (dlg) { e.preventDefault(); dlg.close(); } }
  });
  // Clicking the backdrop closes a modal (not while it is busy).
  document.addEventListener('mousedown', function (e) {
    if (e.target.tagName === 'DIALOG' && e.target.open && !e.target.hasAttribute('data-static')) {
      var r = e.target.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) e.target.close();
    }
  });
  // Content loaded into the shared modal or drawer opens it; its first element may carry data-modal-title / data-modal-size.
  document.body.addEventListener('htmx:afterSwap', function (e) {
    var t = e.detail.target;
    if (!t || !t.id) return;
    if (t.id === 'hj-modal-body' || t.id === 'hj-drawer-body') {
      var d = t.closest('dialog');
      var first = t.firstElementChild;
      var title = first && first.getAttribute('data-modal-title');
      var titleEl = $('.hj-modal-title', d);
      if (titleEl) titleEl.textContent = title || '';
      d.classList.remove('sm', 'lg', 'xl');
      var size = first && first.getAttribute('data-modal-size');
      if (size) d.classList.add(size);
      if (!d.open) d.showModal();
    }
  });
  document.addEventListener('close', function (e) {
    var d = e.target;
    if (d.id === 'hj-modal' || d.id === 'hj-drawer') { var b = $('.hj-modal-body', d); if (b) b.innerHTML = ''; }
  }, true);

  /* ------------------------------------------------------------ shell: mobile nav, user menu, theme */
  document.addEventListener('click', function (e) {
    var shell = $('.hj-shell');
    if (e.target.closest('[data-nav-toggle]') && shell) { shell.toggleAttribute('data-nav-open'); return; }
    if (e.target.closest('.hj-backdrop') && shell) { shell.removeAttribute('data-nav-open'); return; }
    var mb = e.target.closest('[data-menu-toggle]');
    $$('.hj-menu[data-open]').forEach(function (m) { if (!mb || m.id !== mb.getAttribute('data-menu-toggle')) { m.removeAttribute('data-open'); m.hidden = true; } });
    if (mb) {
      var m = document.getElementById(mb.getAttribute('data-menu-toggle'));
      if (m) { var open = m.hidden; m.hidden = !open; m.toggleAttribute('data-open', open); mb.setAttribute('aria-expanded', String(open)); }
    }
    if (e.target.closest('[data-theme-toggle]')) {
      var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { localStorage.setItem('hj-theme', next); } catch (err) { /* ignore */ }
      $$('[data-theme-label]').forEach(function (l) { l.textContent = next === 'dark' ? 'Light mode' : 'Dark mode'; });
      $$('[data-theme-icon]').forEach(function (i) { i.className = 'ti ti-' + (next === 'dark' ? 'sun' : 'moon'); });
    }
  });
  function syncThemeLabels() {
    var dark = document.documentElement.getAttribute('data-theme') === 'dark';
    $$('[data-theme-label]').forEach(function (l) { l.textContent = dark ? 'Light mode' : 'Dark mode'; });
    $$('[data-theme-icon]').forEach(function (i) { i.className = 'ti ti-' + (dark ? 'sun' : 'moon'); });
  }
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function (q) {
      var saved = null; try { saved = localStorage.getItem('hj-theme'); } catch (e) { /* ignore */ }
      if (!saved) { document.documentElement.setAttribute('data-theme', q.matches ? 'dark' : 'light'); syncThemeLabels(); }
    });
  }

  /* ------------------------------------------------------------ command palette (Cmd/Ctrl+K) */
  var palette = { items: [], sel: 0, shown: [] };
  function loadPalette() {
    var s = $('#hj-palette-data');
    try { palette.items = s ? JSON.parse(s.textContent) : []; } catch (e) { palette.items = []; }
  }
  function hl(label, q) {
    if (!q) return esc(label);
    var i = label.toLowerCase().indexOf(q.toLowerCase());
    return i < 0 ? esc(label) : esc(label.slice(0, i)) + '<mark>' + esc(label.slice(i, i + q.length)) + '</mark>' + esc(label.slice(i + q.length));
  }
  function renderPalette() {
    var input = $('#hj-palette-input'); var list = $('#hj-palette-list');
    if (!input || !list) return;
    var q = input.value.trim().toLowerCase();
    var words = q.split(/\s+/).filter(Boolean);
    palette.shown = palette.items.filter(function (i) {
      var hay = (i.label + ' ' + i.group + ' ' + (i.keywords || '')).toLowerCase();
      return words.every(function (w) { return hay.indexOf(w) >= 0; });
    }).slice(0, 12);
    if (palette.sel >= palette.shown.length) palette.sel = 0;
    list.innerHTML = palette.shown.length ? palette.shown.map(function (i, k) {
      return '<li><a href="' + esc(i.url) + '" role="option" aria-selected="' + (k === palette.sel) + '" data-k="' + k + '"><i class="ti ti-' + esc(i.icon) + '" aria-hidden="true"></i><span>' +
        hl(i.label, q) + '</span><span class="group">' + esc(i.group) + '</span></a></li>';
    }).join('') : '<li class="none">No matching page</li>';
  }
  H.openPalette = function () {
    var d = $('#hj-palette'); if (!d) return;
    if (!palette.items.length) loadPalette();
    var input = $('#hj-palette-input'); input.value = ''; palette.sel = 0; renderPalette();
    if (!d.open) d.showModal();
    input.focus();
  };
  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); H.openPalette(); return; }
    var d = $('#hj-palette');
    if (!d || !d.open || !e.target.closest('#hj-palette')) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      var n = palette.shown.length; if (!n) return;
      palette.sel = (palette.sel + (e.key === 'ArrowDown' ? 1 : n - 1)) % n; renderPalette();
      var a = $('#hj-palette-list a[aria-selected="true"]'); if (a) a.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter') {
      var it = palette.shown[palette.sel]; if (it) { e.preventDefault(); window.location.href = it.url; }
    }
  });
  document.addEventListener('input', function (e) { if (e.target.id === 'hj-palette-input') { palette.sel = 0; renderPalette(); } });
  document.addEventListener('click', function (e) { if (e.target.closest('[data-palette-open]')) { e.preventDefault(); H.openPalette(); } });

  /* ------------------------------------------------------------ offline banner and offline clock-ins (lib/offlinePunch.ts) */
  var KEY = 'hajiri.offlinePunches';
  H.offline = {
    queued: function (userId) {
      try { var all = JSON.parse(localStorage.getItem(KEY) || '[]'); return Array.isArray(all) ? all.filter(function (p) { return !userId || p.userId === userId; }) : []; } catch (e) { return []; }
    },
    save: function (list) { try { localStorage.setItem(KEY, JSON.stringify(list)); } catch (e) { /* ignore */ } updateOffline(); },
    queue: function (userId, direction) {
      var p = { id: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now())), direction: direction, at: new Date().toISOString(), localDate: H.todayNpt(), userId: userId };
      H.offline.save(H.offline.queued().concat([p]).slice(-20));
      return p;
    },
  };
  var flushing = false;
  function updateOffline() {
    var bar = $('#hj-offline'); if (!bar) return;
    var uid = document.body.getAttribute('data-user-id');
    var q = uid ? H.offline.queued(uid) : [];
    var online = navigator.onLine !== false;
    if (online && !q.length) { bar.hidden = true; return; }
    bar.hidden = false;
    bar.classList.toggle('sending', online);
    $('i', bar).className = 'ti ti-' + (online ? 'cloud-upload' : 'cloud-off');
    $('span', bar).textContent = online ? 'Sending clock-ins saved while you were offline…'
      : 'You are offline. Pages you opened still show; nothing new can be saved.' + (q.length ? ' ' + q.length + ' clock-' + (q.length === 1 ? 'in is' : 'ins are') + ' saved on this device and will be sent when you are back online.' : '');
    var url = document.body.getAttribute('data-offline-flush-url');
    if (online && q.length && url && !flushing) flush(uid, url, q);
  }
  function flush(uid, url, q) {
    // One at a time, like flushOfflinePunches: each one leaves the queue as soon as the server has answered for it
    // (sent or refused), so a connection lost half-way never sends a clock-in twice.
    flushing = true;
    var csrf = (document.body.getAttribute('hx-headers') || '').match(/"X-CSRFToken"\s*:\s*"([^"]+)"/);
    var sent = 0; var refused = [];
    var drop = function (id) { H.offline.save(H.offline.queued().filter(function (p) { return p.id !== id; })); };
    var done = function () {
      flushing = false;
      if (sent) H.toast(sent + ' clock-' + (sent === 1 ? 'in' : 'ins') + ' made while offline ' + (sent === 1 ? 'was' : 'were') + ' sent to your manager as ' + (sent === 1 ? 'a correction' : 'corrections') + '.', 'ok', 'Back online');
      refused.forEach(function (m) { H.toast(m, 'err', 'An offline clock-in was not accepted'); });
    };
    var next = function (i) {
      if (i >= q.length) { done(); return; }
      var p = q[i];
      fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRFToken': csrf ? csrf[1] : '' }, body: JSON.stringify(p) })
        .then(function (r) {
          if (r.ok) { sent++; drop(p.id); return null; }
          if (r.status >= 500) throw new Error('retry');
          return r.json().catch(function () { return {}; }).then(function (b) { refused.push(b.error || 'refused'); drop(p.id); });
        })
        .then(function () { next(i + 1); })
        .catch(function () { done(); /* no connection or a server error: the rest stay for the next try */ });
    };
    next(0);
  }
  /* ------------------------------------------------------------ clock-in device token (lib/deviceToken.ts) */
  // Random, kept in local storage, sent only with clock-in requests (elements marked data-device-token) once this
  // browser is registered. H.deviceToken() creates one (the "register this device" page posts it).
  var DEVICE_KEY = 'hajiri.deviceToken';
  H.existingDeviceToken = function () { try { return localStorage.getItem(DEVICE_KEY); } catch (e) { return null; } };
  H.deviceToken = function () {
    var have = H.existingDeviceToken(); if (have) return have;
    var bytes = new Uint8Array(32); crypto.getRandomValues(bytes);
    var bin = ''; for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    var token = btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    try { localStorage.setItem(DEVICE_KEY, token); } catch (e) { /* private mode: the token lasts for this page only */ }
    return token;
  };
  H.deviceHeaders = function () { var t = H.existingDeviceToken(); return t ? { 'X-Device-Token': t } : {}; };
  document.body.addEventListener('htmx:configRequest', function (e) {
    var el = e.detail.elt;
    if (!el || !el.closest || !el.closest('[data-device-token]')) return;
    var t = H.existingDeviceToken(); if (t) e.detail.headers['X-Device-Token'] = t;
    // A field named device_token is filled with a new or existing token (device registration forms).
    if (Object.prototype.hasOwnProperty.call(e.detail.parameters || {}, 'device_token') && !e.detail.parameters.device_token) {
      e.detail.parameters.device_token = H.deviceToken();
    }
  });

  window.addEventListener('online', updateOffline);
  window.addEventListener('offline', updateOffline);

  /* ------------------------------------------------------------ chart tooltips: any element with data-tip */
  var tip = null;
  document.addEventListener('mousemove', function (e) {
    var t = e.target.closest && e.target.closest('[data-tip]');
    if (!t) { if (tip) tip.hidden = true; return; }
    if (!tip) { tip = document.createElement('div'); tip.className = 'hj-tip'; document.body.appendChild(tip); }
    tip.textContent = t.getAttribute('data-tip'); tip.hidden = false;
    var x = e.clientX + 12; var w = tip.offsetWidth;
    if (x + w > window.innerWidth - 8) x = e.clientX - w - 12;
    tip.style.left = x + 'px'; tip.style.top = (e.clientY - 34) + 'px';
  });

  /* ------------------------------------------------------------ Bikram Sambat calendar (window.HJ_BS from /_/web/bs-calendar.js) */
  var BS = (H.bs = {});
  var AD_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  BS.AD_MONTHS = AD_MONTHS; BS.WEEKDAYS = WEEKDAYS;
  function data() { return window.HJ_BS; }
  function years() { return Object.keys(data().months).map(Number).sort(function (a, b) { return a - b; }); }
  BS.range = function () { var y = years(); return { min: y[0], max: y[y.length - 1] }; };
  BS.daysIn = function (y, m) { var r = data().months[String(y)]; if (!r) throw new Error('range'); return r[m - 1]; };
  function ordinal(y, m, d) {
    var ys = years(); var n = 0;
    for (var i = 0; i < ys.length && ys[i] < y; i++) n += data().months[String(ys[i])].reduce(function (a, b) { return a + b; }, 0);
    var row = data().months[String(y)];
    for (var k = 0; k < m - 1; k++) n += row[k];
    return n + d - 1;
  }
  var DAY = 86400000;
  function isoToMs(iso) { return Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10)); }
  function msToIso(ms) { return new Date(ms).toISOString().slice(0, 10); }
  BS.valid = function (y, m, d) { var r = data().months[String(y)]; return !!r && m >= 1 && m <= 12 && d >= 1 && d <= r[m - 1]; };
  BS.toAd = function (y, m, d) {
    if (!BS.valid(y, m, d)) throw new Error('invalid BS date');
    var a = data().anchorBs;
    return msToIso(isoToMs(data().anchorAd) + (ordinal(y, m, d) - ordinal(a[0], a[1], a[2])) * DAY);
  };
  BS.fromAd = function (iso) {
    var a = data().anchorBs;
    var target = ordinal(a[0], a[1], a[2]) + Math.round((isoToMs(iso) - isoToMs(data().anchorAd)) / DAY);
    if (target < 0) return null;
    var ys = years(); var n = 0;
    for (var i = 0; i < ys.length; i++) {
      var row = data().months[String(ys[i])];
      for (var m = 0; m < 12; m++) { if (target < n + row[m]) return { year: ys[i], month: m + 1, day: target - n + 1 }; n += row[m]; }
    }
    return null;
  };
  BS.shift = function (iso, days) { return msToIso(isoToMs(iso) + days * DAY); };
  BS.weekday = function (iso) { return new Date(isoToMs(iso)).getUTCDay(); };
  BS.fmt = function (iso) {
    if (!iso) return '—';
    var b = BS.fromAd(iso);
    return b ? b.day + ' ' + data().names[b.month - 1] + ' ' + b.year : BS.gregorian(iso);
  };
  BS.gregorian = function (iso) { return iso ? +iso.slice(8, 10) + ' ' + AD_MONTHS[+iso.slice(5, 7) - 1] + ' ' + iso.slice(0, 4) : '—'; };
  BS.long = function (iso) { return WEEKDAYS[BS.weekday(iso)] + ', ' + BS.fmt(iso); };
  BS.grid = function (y, m) {
    var cells = []; var first = BS.toAd(y, m, 1); var lead = BS.weekday(first);
    for (var i = lead; i > 0; i--) { var ad = BS.shift(first, -i); var b = BS.fromAd(ad); if (b) cells.push({ ad: ad, bs: b, wd: BS.weekday(ad), inMonth: false }); }
    for (var d = 1; d <= BS.daysIn(y, m); d++) { var a2 = BS.toAd(y, m, d); cells.push({ ad: a2, bs: { year: y, month: m, day: d }, wd: BS.weekday(a2), inMonth: true }); }
    while (cells.length % 7 !== 0) { var ad3 = BS.shift(cells[cells.length - 1].ad, 1); var b3 = BS.fromAd(ad3); if (!b3) break; cells.push({ ad: ad3, bs: b3, wd: BS.weekday(ad3), inMonth: false }); }
    return cells;
  };
  BS.inRange = function (y) { var r = BS.range(); return y >= r.min && y <= r.max; };
  BS.prev = function (y, m) { var n = m === 1 ? { year: y - 1, month: 12 } : { year: y, month: m - 1 }; return BS.inRange(n.year) ? n : { year: y, month: m }; };
  BS.next = function (y, m) { var n = m === 12 ? { year: y + 1, month: 1 } : { year: y, month: m + 1 }; return BS.inRange(n.year) ? n : { year: y, month: m }; };
  H.todayNpt = function () { return new Date(Date.now() + 345 * 60000).toISOString().slice(0, 10); };
  BS.adSpan = function (a, b) {
    var am = AD_MONTHS[+a.slice(5, 7) - 1]; var bm = AD_MONTHS[+b.slice(5, 7) - 1];
    return a.slice(0, 4) === b.slice(0, 4) ? am + '–' + bm + ' ' + a.slice(0, 4) : am + ' ' + a.slice(0, 4) + '–' + bm + ' ' + b.slice(0, 4);
  };

  /* Clickable table rows: <tr data-href="..."> (links, buttons and inputs inside the row keep their own action). */
  document.addEventListener('click', function (e) {
    var tr = e.target.closest('tr[data-href]');
    if (!tr || e.target.closest('a,button,input,label,select,textarea')) return;
    if (e.metaKey || e.ctrlKey) window.open(tr.getAttribute('data-href'), '_blank'); else window.location.href = tr.getAttribute('data-href');
  });

  /* ------------------------------------------------------------ copy and download helpers */
  H.copy = function (text, btn) {
    var done = function () { if (btn) { var t = btn.textContent; btn.textContent = 'Copied'; setTimeout(function () { btn.textContent = t; }, 1500); } };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done);
    else { var ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); done(); } catch (e) { /* ignore */ } ta.remove(); }
  };
  H.download = function (text, filename, type) {
    var url = URL.createObjectURL(new Blob([text], { type: type || 'text/plain' }));
    var a = document.createElement('a'); a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  };
  document.addEventListener('click', function (e) {
    var c = e.target.closest('[data-copy]');
    if (c) { e.preventDefault(); H.copy(c.getAttribute('data-copy'), c); }
    var d = e.target.closest('[data-download-text]');
    if (d) {
      e.preventDefault();
      var src = document.getElementById(d.getAttribute('data-download-text'));
      H.download(src ? src.textContent : '', d.getAttribute('data-filename') || 'download.txt');
    }
  });

  /* ------------------------------------------------------------ Alpine components */
  document.addEventListener('alpine:init', function () {
    var A = window.Alpine;

    /* BsDatePicker.tsx: Nepali month grid, BS day numbers with the Gregorian day in the corner, typed BS fallback. */
    A.data('hjDate', function (opts) {
      return {
        value: opts.value || '', min: opts.min || '', max: opts.max || '', holidays: opts.holidays || {}, required: !!opts.required,
        open: false, mode: 'days', view: { year: 2083, month: 1 }, focus: null, typed: '', typedErr: '',
        init: function () { this.reset(); },
        reset: function () {
          var b = (this.value && BS.fromAd(this.value)) || BS.fromAd(H.todayNpt()) || { year: BS.range().min, month: 1 };
          this.view = { year: b.year, month: b.month };
        },
        get label() { return this.value ? BS.fmt(this.value) : ''; },
        get names() { return window.HJ_BS.names; },
        get weekdays() { return WEEKDAYS; },
        get cells() { try { return BS.grid(this.view.year, this.view.month); } catch (e) { return []; } },
        get title() { return window.HJ_BS.names[this.view.month - 1] + ' ' + this.view.year; },
        get adLabel() { var c = this.cells.filter(function (x) { return x.inMonth; }); return c.length ? BS.adSpan(c[0].ad, c[c.length - 1].ad) : ''; },
        get years() { var r = BS.range(); var out = []; for (var y = r.max; y >= r.min; y--) out.push(y); return out; },
        get footNote() {
          if (window.HJ_BS.projected.indexOf(this.view.year) >= 0) return 'Projected year: may shift when the official calendar is published';
          return this.value ? BS.gregorian(this.value) + ' in the Gregorian calendar' : 'Saturdays in red, holidays dotted';
        },
        today: H.todayNpt(),
        inBounds: function (ad) { return (!this.min || ad >= this.min) && (!this.max || ad <= this.max); },
        toggle: function () { if (this.open) { this.open = false; return; } this.reset(); this.mode = 'days'; this.focus = this.value || this.today; var b = this.value && BS.fromAd(this.value); this.typed = b ? b.year + '-' + String(b.month).padStart(2, '0') + '-' + String(b.day).padStart(2, '0') : ''; this.typedErr = ''; this.open = true; var self = this; this.$nextTick(function () { var f = self.$root.querySelector('[data-focus="true"]'); if (f) f.focus(); }); },
        pick: function (ad) { if (!this.inBounds(ad)) return; this.value = ad; this.open = false; this.changed(); },
        clear: function () { this.value = ''; this.changed(); },
        changed: function () { var self = this; this.$nextTick(function () { var i = self.$refs.input; if (i) { i.dispatchEvent(new Event('change', { bubbles: true })); i.dispatchEvent(new Event('input', { bubbles: true })); } }); },
        prev: function () { this.view = BS.prev(this.view.year, this.view.month); },
        next: function () { this.view = BS.next(this.view.year, this.view.month); },
        setMonth: function (m) { this.view = { year: this.view.year, month: m }; this.mode = 'days'; },
        cellClass: function (c) {
          return { out: !c.inMonth, in: c.inMonth, sat: c.wd === 6, today: c.ad === this.today && c.ad !== this.value, sel: c.ad === this.value, hol: !!this.holidays[c.ad] };
        },
        cellLabel: function (c) { return BS.long(c.ad) + ' (' + BS.gregorian(c.ad) + ')' + (this.holidays[c.ad] ? ', ' + this.holidays[c.ad] : ''); },
        corner: function (c) { var d = +c.ad.slice(8); return d === 1 ? AD_MONTHS[+c.ad.slice(5, 7) - 1] : d; },
        move: function (days) {
          var nx = BS.shift(this.focus || this.today, days); var b = BS.fromAd(nx); if (!b) return;
          this.focus = nx; if (b.year !== this.view.year || b.month !== this.view.month) this.view = { year: b.year, month: b.month };
          var self = this; this.$nextTick(function () { var f = self.$root.querySelector('[data-focus="true"]'); if (f) f.focus(); });
        },
        key: function (e) {
          var step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
          if (e.key in step) { e.preventDefault(); this.move(step[e.key]); }
          else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this.pick(this.focus || this.today); }
          else if (e.key === 'PageUp') { e.preventDefault(); this.prev(); }
          else if (e.key === 'PageDown') { e.preventDefault(); this.next(); }
          else if (e.key === 'Escape') { this.open = false; }
        },
        commitTyped: function (fromEnter) {
          if (!this.typed.trim()) { this.typedErr = ''; if (fromEnter) this.open = false; return; }
          var m = /^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*$/.exec(this.typed);
          try {
            if (!m) throw new Error('format');
            var ad = BS.toAd(+m[1], +m[2], +m[3]);
            if (!this.inBounds(ad)) { this.typedErr = 'That date is outside the allowed range'; return; }
            this.typedErr = '';
            if (ad !== this.value) this.pick(ad); else if (fromEnter) this.open = false;
          } catch (err) { this.typedErr = 'Use the Nepali date as YYYY-MM-DD, for example 2083-06-28'; }
        },
      };
    });

    /* EmployeePicker.tsx: type to search over HTMX; picking fills the hidden input. */
    A.data('hjCombo', function (opts) {
      return {
        value: opts.value || '', label: opts.label || '', open: false, active: -1,
        choose: function (id, label) { this.value = id; this.label = label; this.open = false; var self = this; this.$nextTick(function () { self.$refs.input.dispatchEvent(new Event('change', { bubbles: true })); }); },
        clear: function () { this.choose('', ''); this.$refs.search.focus(); },
        keys: function (e) {
          var opts2 = this.$root.querySelectorAll('[data-option]');
          if (e.key === 'ArrowDown') { e.preventDefault(); this.open = true; this.active = Math.min(opts2.length - 1, this.active + 1); }
          else if (e.key === 'ArrowUp') { e.preventDefault(); this.active = Math.max(0, this.active - 1); }
          else if (e.key === 'Enter') { if (this.open && opts2[this.active]) { e.preventDefault(); opts2[this.active].click(); } }
          else if (e.key === 'Escape') { this.open = false; }
          opts2.forEach(function (o, i) { o.setAttribute('aria-selected', String(i === this.active)); }, this);
        },
      };
    });

    /* FileUpload.tsx: upload straight away (max 4 MB) and keep the stored file id. */
    A.data('hjUpload', function (opts) {
      return {
        id: opts.value || '', filename: opts.filename || '', busy: false, url: opts.url, purpose: opts.purpose,
        pick: function () { this.$refs.file.click(); },
        upload: function (e) {
          var f = e.target.files && e.target.files[0]; e.target.value = '';
          if (!f) return;
          if (f.size > 4 * 1024 * 1024) { H.toast('The file is too large (max 4 MB).', 'err', 'Upload failed'); return; }
          var fd = new FormData(); fd.append('file', f); fd.append('purpose', this.purpose);
          var csrf = (document.body.getAttribute('hx-headers') || '').match(/"X-CSRFToken"\s*:\s*"([^"]+)"/);
          var self = this; this.busy = true;
          fetch(this.url, { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'Accept': 'application/json', 'X-CSRFToken': csrf ? csrf[1] : '' } })
            .then(function (r) { return r.json().catch(function () { return {}; }).then(function (b) { if (!r.ok) throw new Error(b.error || 'Upload failed (' + r.status + ')'); return b; }); })
            .then(function (b) { self.id = b.file.id; self.filename = b.file.filename; self.$nextTick(function () { self.$refs.input.dispatchEvent(new Event('change', { bubbles: true })); }); })
            .catch(function (err) { H.toast(err.message || 'No connection. Please check your network and try again.', 'err', 'Upload failed'); })
            .finally(function () { self.busy = false; });
        },
        remove: function () { this.id = ''; this.filename = ''; },
      };
    });

    /* CameraCapture.tsx: front camera selfie, or the phone camera through a file picker. JPEG <= 480 px wide, < 280 KB. */
    A.data('hjCamera', function () {
      var MAX_W = 480; var MAX_BYTES = 280 * 1024;
      function toJpeg(src, w, h, mirror) {
        var scale = Math.min(1, MAX_W / w); var c = document.createElement('canvas');
        c.width = Math.round(w * scale); c.height = Math.round(h * scale);
        var ctx = c.getContext('2d'); if (mirror) { ctx.translate(c.width, 0); ctx.scale(-1, 1); }
        ctx.drawImage(src, 0, 0, c.width, c.height);
        var q = 0.8; var out = c.toDataURL('image/jpeg', q);
        while (out.length * 0.75 > MAX_BYTES && q > 0.35) { q -= 0.1; out = c.toDataURL('image/jpeg', q); }
        return out;
      }
      return {
        phase: 'starting', photo: '', problem: '', stream: null,
        start: function () {
          var self = this; this.photo = ''; this.problem = ''; this.phase = 'starting';
          if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { this.problem = window.isSecureContext ? 'This browser cannot open the camera.' : 'The camera only works over a secure (HTTPS) connection.'; this.phase = 'unavailable'; return; }
          navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user', width: { ideal: 640 }, height: { ideal: 480 } }, audio: false })
            .then(function (s) { self.stream = s; var v = self.$refs.video; v.srcObject = s; return v.play().catch(function () {}); })
            .then(function () { if (self.stream) self.phase = 'live'; })
            .catch(function (e) { self.problem = e && e.name === 'NotAllowedError' ? 'Camera permission was refused. Allow the camera for this site, or take the photo with your phone camera instead.' : 'No camera was found on this device.'; self.phase = 'unavailable'; });
        },
        stop: function () { if (this.stream) this.stream.getTracks().forEach(function (t) { t.stop(); }); this.stream = null; },
        snap: function () { var v = this.$refs.video; if (!v || !v.videoWidth) return; this.photo = toJpeg(v, v.videoWidth, v.videoHeight, true); this.stop(); this.phase = 'captured'; },
        fromFile: function (e) {
          var f = e.target.files && e.target.files[0]; e.target.value = ''; if (!f) return;
          var self = this; var url = URL.createObjectURL(f); var img = new Image();
          img.onload = function () { self.photo = toJpeg(img, img.naturalWidth, img.naturalHeight, false); URL.revokeObjectURL(url); self.phase = 'captured'; };
          img.onerror = function () { self.problem = 'That file is not a photo we can read.'; URL.revokeObjectURL(url); };
          img.src = url;
        },
        confirm: function () {
          this.$refs.out.value = this.photo;
          this.$refs.out.dispatchEvent(new Event('change', { bubbles: true }));
          this.$root.dispatchEvent(new CustomEvent('hj:photo', { bubbles: true, detail: { photo: this.photo } }));
          var form = this.$refs.out.form; var root = this.$root;
          this.stop(); if (root.close) root.close();
          if (form && root.hasAttribute('data-submit')) { if (form.requestSubmit) form.requestSubmit(); else form.submit(); }
        },
      };
    });
  });

  /* ------------------------------------------------------------ boot */
  document.addEventListener('DOMContentLoaded', function () {
    syncThemeLabels();
    var f = $('#hj-flash');
    if (f) { try { JSON.parse(f.textContent).forEach(function (t) { H.toast(t.message, t.tone, t.title); }); } catch (e) { /* ignore */ } }
    updateOffline();
    if ('serviceWorker' in navigator && document.body.hasAttribute('data-sw')) {
      window.addEventListener('load', function () { navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {}); });
    }
  });
})();
