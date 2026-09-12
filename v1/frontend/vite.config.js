import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
// /api 统一代理到后端,前端无需关心跨域。
export default defineConfig({
    plugins: [vue()],
    server: {
        port: 5173,
        proxy: {
            '/api': {
                target: 'http://localhost:8000',
                changeOrigin: true,
            },
        },
    },
});
