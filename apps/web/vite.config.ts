import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const api = process.env.API_URL ?? 'http://localhost:3000';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Keep the browser's Host header: the API refuses cookie writes whose Origin does not match the host it sees.
    proxy: { '/api': { target: api, changeOrigin: false }, '/health': { target: api, changeOrigin: false } },
  },
  build: { outDir: 'dist', sourcemap: false },
});
