/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// The portal is served by the control plane (control_plane.auth.build_portal_app,
// CP_PORTAL_DIST) on its own hostname, LOOPYARD_PORTAL_URL. In dev, Vite proxies
// the CP routes to CP_DEV_TARGET (a local control plane on a high port).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const proxy = { target: env.CP_DEV_TARGET || 'http://127.0.0.1:8841', changeOrigin: false };
  return {
    plugins: [react()],
    define: {
      __PORTAL_URL__: JSON.stringify((env.LOOPYARD_PORTAL_URL || '').replace(/\/+$/, '')),
    },
    server: {
      port: Number(env.PORT || 5183),
      host: '127.0.0.1',
      strictPort: true,
      proxy: { '/api': proxy, '/login': proxy, '/oauth': proxy, '/logout': proxy },
    },
    test: { environment: 'jsdom' },
  };
});
