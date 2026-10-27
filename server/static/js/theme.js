/* Runs before the page paints: light or dark from the saved choice, else the system setting. */
(function () {
  var t = null;
  try { t = localStorage.getItem('hj-theme'); } catch (e) { /* storage blocked */ }
  if (t !== 'light' && t !== 'dark') t = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', t);
})();
