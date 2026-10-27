/*
 * Home clock-in card (HomePage.tsx doPunch, no inline script: the CSP forbids it).
 * Hajiri.homeOffline(form): no connection, so the tap is kept on this device (lib/offlinePunch.ts); app.js sends it to
 * the manager as a missed clock-in or clock-out once the browser is back online.
 */
(function () {
  if (window.__hjHome) return;
  window.__hjHome = true;
  function nptHm() { return new Date(Date.now() + 345 * 60000).toISOString().slice(11, 16); }
  function install() {
    var H = window.Hajiri; if (!H || H.homeOffline) return;
    H.homeOffline = function (form) {
      var uid = document.body.getAttribute('data-user-id');
      var d = form.querySelector('input[name="direction"]');
      var direction = d && d.value === 'out' ? 'out' : 'in';
      if (!uid || !H.offline) return;
      H.offline.queue(uid, direction);
      var dlg = form.querySelector('dialog[open]'); if (dlg && dlg.close) dlg.close();
      H.toast('Saved on this device at ' + nptHm() + '. When you are back online it is sent to your manager as a missed '
        + (direction === 'in' ? 'clock-in' : 'clock-out') + ' to approve.', 'info', "You're offline");
    };
  }
  install();
  document.addEventListener('DOMContentLoaded', install);
})();
