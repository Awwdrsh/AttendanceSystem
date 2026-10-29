import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const api = process.env.API_URL ?? 'http://localhost:3000';

// The production build goes straight into the folder Django serves (server/frontend_build/, committed to git so an
// install needs only Python). Django answers index.html for every page address and WhiteNoise serves the rest of the
// folder at the site root: /assets/* (content-hashed, cached for a year), sw.js, the manifest and the icons.
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: 5173,
    // Keep the browser's Host header: Django refuses cookie writes whose Origin does not match the host it sees.
    proxy: {
      '/api': { target: api, changeOrigin: false },
      '/health': { target: api, changeOrigin: false },
    },
  },
  build: {
    outDir: fileURLToPath(new URL('../server/frontend_build', import.meta.url)),
    emptyOutDir: true,
    sourcemap: false,
    assetsDir: 'assets',
    chunkSizeWarningLimit: 800,
  },
});
