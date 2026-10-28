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
