import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * Vite 配置。
 *
 * `/api` 走代理指向本地后端，前端代码里因此可以只写相对路径（`` 作为 BASE_URL），
 * 既避免了开发期 CORS，也让「切真实接口」只需要改 `VITE_USE_MOCK` 一个开关。
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  // `vite preview` 跑的是 build 产物（已切真实接口），同样需要代理到后端
  preview: {
    port: 4173,
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
});
