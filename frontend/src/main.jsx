import '@fontsource-variable/manrope';
import '@fontsource-variable/jetbrains-mono';
import '@mantine/core/styles.css';
import '@mantine/notifications/styles.css';
import '@mantine/spotlight/styles.css';
import './styles.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { MantineProvider } from '@mantine/core';
import { Notifications } from '@mantine/notifications';
import { BrowserRouter } from 'react-router-dom';
import { theme } from './theme';
import { AuthProvider } from './auth/AuthProvider';
import { App } from './App';

document.documentElement.lang = 'en';

// Installable app: the service worker caches the app shell only (never API data). Not in development, where it would cache Vite's modules.
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => undefined);
  });
}

// After a new release the old page's lazily loaded files are gone from the server. Load the new release instead of
// showing a blank page; at most once a minute, so a file that is really missing cannot cause a reload loop.
window.addEventListener('vite:preloadError', (event) => {
  let last = 0;
  try {
    last = Number(sessionStorage.getItem('hj-reloaded-at')) || 0;
  } catch {
    /* storage blocked: still reload once */
  }
  if (Date.now() - last < 60000) return;
  try {
    sessionStorage.setItem('hj-reloaded-at', String(Date.now()));
  } catch {
    /* ignore */
  }
  event.preventDefault();
  window.location.reload();
});

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="light">
      <Notifications position="bottom-right" />
      <AuthProvider>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AuthProvider>
    </MantineProvider>
  </StrictMode>,
);
