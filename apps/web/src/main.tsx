import '@mantine/core/styles.css';
import '@mantine/notifications/styles.css';
import { StrictMode, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { MantineProvider } from '@mantine/core';
import { Notifications } from '@mantine/notifications';
import { BrowserRouter } from 'react-router-dom';
import { theme } from './theme';
import { AuthProvider } from './auth/AuthProvider';
import { bilingual, I18nContext, translate, type Lang, type Numerals } from './i18n';
import { App } from './App';

function I18nProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLangState] = useState<Lang>(() => (localStorage.getItem('hajiri.lang') as Lang) || 'en');
  const [numerals, setNumeralsState] = useState<Numerals>(() => (localStorage.getItem('hajiri.numerals') as Numerals) || 'latin');
  const value = useMemo(() => ({
    lang, numerals,
    setLang: (l: Lang) => { localStorage.setItem('hajiri.lang', l); setLangState(l); document.documentElement.lang = l; },
    setNumerals: (n: Numerals) => { localStorage.setItem('hajiri.numerals', n); setNumeralsState(n); },
    t: (k: Parameters<typeof translate>[1]) => translate(lang, k),
    both: bilingual,
  }), [lang, numerals]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="auto">
      <Notifications position="top-right" />
      <I18nProvider>
        <AuthProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </AuthProvider>
      </I18nProvider>
    </MantineProvider>
  </StrictMode>,
);
