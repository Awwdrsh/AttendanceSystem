/*
 * Location for field visits and work locations (api/field.ts currentPosition), with no inline script (the CSP forbids
 * it). Loaded by the field pages and fragments; safe to load more than once.
 *  - <form data-geo>: when it appears, the browser's position is asked for and put into its hidden inputs
 *    name="lat", "lng" and "accuracy" (left empty when the browser refuses or times out after 8 s).
 *  - <button data-geo-fill="lat-input-id,lng-input-id">: fills those inputs with the current position (12 s timeout),
 *    with the TS toasts when it is not available or not accurate.
 */
(function () {
  if (window.__hjGeo) return;
  window.__hjGeo = true;
  function position(timeout, done) {
    if (!('geolocation' in navigator)) { done(null); return; }
    navigator.geolocation.getCurrentPosition(function (p) {
      done({ lat: p.coords.latitude, lng: p.coords.longitude, accuracy: Math.round(p.coords.accuracy) });
    }, function () { done(null); }, { enableHighAccuracy: true, timeout: timeout, maximumAge: 30000 });
  }
  function fillForm(form) {
    if (form.__hjGeoAsked) return;
    form.__hjGeoAsked = true;
    position(8000, function (p) {
      if (!p) return;
      ['lat', 'lng', 'accuracy'].forEach(function (k) { var i = form.querySelector('input[name="' + k + '"]'); if (i) i.value = p[k]; });
    });
  }
  function scan(root) {
    (root.querySelectorAll ? root.querySelectorAll('form[data-geo]') : []).forEach(fillForm);
    if (root.matches && root.matches('form[data-geo]')) fillForm(root);
  }
  document.addEventListener('DOMContentLoaded', function () { scan(document); });
  document.addEventListener('htmx:load', function (e) { scan(e.target); });
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-geo-fill]');
    if (!b) return;
    e.preventDefault();
    var ids = b.getAttribute('data-geo-fill').split(',');
    b.setAttribute('aria-busy', 'true');
    position(12000, function (p) {
      b.removeAttribute('aria-busy');
      var toast = window.Hajiri && window.Hajiri.toast;
      if (!p) { if (toast) toast('Your location is not available. Allow location access, or type the coordinates.', 'info'); return; }
      var lat = document.getElementById(ids[0]); var lng = document.getElementById(ids[1]);
      if (lat) { lat.value = p.lat.toFixed(6); lat.dispatchEvent(new Event('input', { bubbles: true })); }
      if (lng) { lng.value = p.lng.toFixed(6); lng.dispatchEvent(new Event('input', { bubbles: true })); }
      if (p.accuracy > 100 && toast) toast('Location accuracy is about ' + p.accuracy + ' m. Stand outside or near a window for a better fix.', 'info');
    });
  });
  scan(document);
})();
