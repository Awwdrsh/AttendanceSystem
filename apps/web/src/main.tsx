import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

// Placeholder entry; the application shell lives in src/App.tsx once built.
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <main style={{ fontFamily: 'Inter, system-ui, sans-serif', padding: 24 }}>
      <h1>Hajiri · हाजिरी</h1>
      <p>Web app build pending.</p>
    </main>
  </StrictMode>,
);
