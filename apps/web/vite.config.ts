import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const api = process.env.API_URL ?? 'http://localhost:3000';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': api, '/health': api },
  },
  build: { outDir: 'dist', sourcemap: false },
});
