import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const backendTarget = process.env.VITE_CHAT_BACKEND || 'http://localhost:8000';
const proxy = {
  '/api': {
    target: backendTarget,
    changeOrigin: true,
  },
};

// The dev server proxies /api to the Kyrex Cloud backend so the standalone
// frontend can run on its own port (5173) while talking to the real backend
// without CORS friction during local development. `vite preview` gets the
// same proxy so the production build can be smoke-tested identically.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy,
  },
  preview: {
    port: 4173,
    proxy,
  },
});
