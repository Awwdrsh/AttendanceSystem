/*
 * The kiosk tablet (KioskPage.tsx) as an Alpine component, with no inline script (the CSP forbids it). The tablet pairs
 * once with a code from HR and keeps the kiosk key in local storage; every call sends it in X-Kiosk-Key. Each person
 * enters their code and PIN (or shows their QR badge), takes a photo and is clocked in or out at server time.
 */
(function () {
  var KEY = 'hajiri.kioskKey';
  var NPT = 345 * 60000;
  var IDLE_MS = 60000;
  var WD = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var store = {
    get: function () { try { return localStorage.getItem(KEY); } catch (e) { return null; } },
    set: function (k) { try { localStorage.setItem(KEY, k); } catch (e) { /* private mode */ } },
    clear: function () { try { localStorage.removeItem(KEY); } catch (e) { /* private mode */ } },
  };
  function ApiError(status, body) { this.status = status; this.body = body || {}; this.message = (body && body.error) || 'Something went wrong. Try again.'; }
  function call(method, path, body) {
    var headers = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
    var k = store.get(); if (k) headers['X-Kiosk-Key'] = k;
    return fetch('/api/v1/kiosk' + path, { method: method, headers: headers, body: body ? JSON.stringify(body) : undefined, credentials: 'omit' })
      .then(function (r) {
        return r.text().then(function (t) {
          var j = {}; try { j = t ? JSON.parse(t) : {}; } catch (e) { j = {}; }
          if (!r.ok) throw new ApiError(r.status, j);
          return j;
        });
      }, function () { throw new ApiError(0, { error: 'No connection. Check the network and try again.' }); });
  }
  function errorText(e) {
    if (e && e.body && e.body.error === 'validation' && e.body.issues && e.body.issues.length) return e.body.issues.map(function (i) { return i.message; }).join('; ');
    return (e && e.message) || 'Something went wrong. Try again.';
  }

  document.addEventListener('alpine:init', function () {
    window.Alpine.data('hjKiosk', function () {
      return {
        state: 'loading', session: null, skew: 0, pairCode: '', empCode: '', pin: '', who: null, busy: false, error: null, done: null,
        clock: { hm: '', s: '', date: '' }, scanProblem: null, _idle: 0, _doneT: 0, _scan: null,
        init: function () {
          var self = this;
          this.tick(); setInterval(function () { self.tick(); }, 1000);
          this.load(); setInterval(function () { self.load(); }, 5 * 60000);
          this.$watch('who', function (w) { clearTimeout(self._idle); if (w) self._idle = setTimeout(function () { self.reset(); }, IDLE_MS); });
          this.$watch('done', function (d) { clearTimeout(self._doneT); if (d) self._doneT = setTimeout(function () { self.done = null; }, 5000); });
        },
        tick: function () {
          var t = new Date(Date.now() + this.skew + NPT).toISOString();
          var iso = t.slice(0, 10);
          var bs = window.Hajiri && window.Hajiri.bs ? window.Hajiri.bs.fmt(iso) : iso;
          this.clock = { hm: t.slice(11, 16), s: t.slice(17, 19), date: WD[new Date(iso + 'T00:00:00Z').getUTCDay()] + ', ' + bs };
        },
        load: function () {
          var self = this;
          if (!store.get()) { this.state = 'unpaired'; return; }
          return call('GET', '/session').then(function (s) {
            self.session = s; self.skew = Date.parse(s.serverTime) - Date.now(); self.state = 'ready';
          }, function (e) {
            if (e.status === 401) { store.clear(); self.state = 'unpaired'; } else { self.error = errorText(e); self.state = 'ready'; }
          });
        },
        locked: function () { var l = this.session && this.session.kiosk.lockedUntil; return !!l && Date.parse(l) > Date.now(); },
        reset: function () { this.who = null; this.empCode = ''; this.pin = ''; this.error = null; var c = document.getElementById('kiosk-camera'); if (c && c.open) c.close(); },
        fail: function (e) { if (e.status === 401) { store.clear(); this.state = 'unpaired'; return; } this.error = errorText(e); },
        pair: function () {
          var self = this; this.busy = true; this.error = null;
          call('POST', '/pair', { code: this.pairCode.trim() }).then(function (r) { store.set(r.key); self.pairCode = ''; return self.load(); },
            function (e) { self.error = errorText(e); }).then(function () { self.busy = false; });
        },
        identify: function (body) {
          var self = this; this.busy = true; this.error = null;
          call('POST', '/identify', body).then(function (r) { self.who = r; self.pin = ''; if (r.selfieRequired) self.openCamera(); },
            function (e) { self.fail(e); self.pin = ''; }).then(function () { self.busy = false; });
        },
        openCamera: function () {
          var c = document.getElementById('kiosk-camera'); if (!c || !this.who) return;
          var label = this.who.nextDirection === 'out' ? 'Clock out' : 'Clock in';
          var title = c.querySelector('.hj-modal-title'); if (title) title.textContent = label + ': ' + this.who.employee.name;
          c.querySelectorAll('button.hj-btn').forEach(function (b) { if (/Clock (in|out)/.test(b.textContent)) b.lastChild.textContent = label; });
          if (c.showModal && !c.open) c.showModal();
        },
        punch: function (selfie) {
          var self = this; if (!this.who) return;
          this.busy = true; this.error = null;
          var body = { ticket: this.who.ticket }; if (selfie) body.selfie = selfie;
          call('POST', '/punch', body).then(function (r) { self.done = { message: r.message, direction: r.direction }; self.reset(); }, function (e) {
            var c = document.getElementById('kiosk-camera'); if (c && c.open) c.close();
            if (e.body && e.body.details && e.body.details.code === 'photo_required') { self.openCamera(); return; }
            self.fail(e); self.who = null;
          }).then(function () { self.busy = false; });
        },
        openScanner: function () {
          var self = this; var d = document.getElementById('kiosk-scanner'); if (!d) return;
          this.scanProblem = null; d.showModal();
          var Detector = window.BarcodeDetector;
          if (!Detector) { this.scanProblem = 'This browser cannot read QR codes. Use your code and PIN instead.'; return; }
          var detector = new Detector({ formats: ['qr_code'] });
          var scan = { stream: null, timer: 0, done: false }; this._scan = scan;
          navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user' }, audio: false }).then(function (stream) {
            scan.stream = stream; var v = self.$refs.scan; v.srcObject = stream; v.play().catch(function () {});
            var tick = function () {
              if (scan.done) return;
              if (v.readyState < 2) { scan.timer = setTimeout(tick, 250); return; }
              detector.detect(v).then(function (codes) {
                var hit = codes.filter(function (c) { return c.rawValue.indexOf('HQ1.') === 0; })[0];
                if (hit) { scan.done = true; d.close(); self.identify({ qr: hit.rawValue }); return; }
                scan.timer = setTimeout(tick, 250);
              }, function () { scan.timer = setTimeout(tick, 250); });
            };
            tick();
          }, function () { self.scanProblem = 'The camera is not available. Use your code and PIN instead.'; });
        },
        stopScanner: function () {
          var s = this._scan; if (!s) return; s.done = true; clearTimeout(s.timer);
          if (s.stream) s.stream.getTracks().forEach(function (t) { t.stop(); });
          this._scan = null;
        },
      };
    });
  });
})();
