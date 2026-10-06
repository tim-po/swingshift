/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode, command }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const target = env.LOOPYARD_PROXY_TARGET || 'http://127.0.0.1:8795';
  const sess = env.LOOPYARD_DASH_SESS;

  const proxy = {
    target,
    changeOrigin: true,
    secure: true,
    // Behind the Telegram gate: attach the session cookie server-side.
    headers: sess ? { Cookie: `dash_sess=${sess}` } : undefined,
  };

  return {
    plugins: [react()],
    // Production build is served by Starlette under /app/ (old dashboard stays at /).
    base: command === 'build' ? '/app/' : '/',
    server: {
      port: Number(env.PORT || 5173),
      host: '127.0.0.1',
      strictPort: true,
      proxy: {
        '/api': proxy,
        '/ws': { ...proxy, ws: true },
        // vendored assets the legacy server hosts (xterm)
        '/static': proxy,
      },
    },
    test: { environment: 'jsdom' },
  };
});
