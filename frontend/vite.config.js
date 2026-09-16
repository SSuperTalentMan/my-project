import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端地址：可用环境变量覆盖（默认走本机 8000 主服务）
const target = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000'

// 需要转发给后端的路径前缀。用 dev server 代理而不是让浏览器直连，
// 是为了彻底绕开 CORS —— 前端和后端在开发期是不同端口。
const proxied = ['/api', '/health', '/version', '/metrics', '/openapi.json', '/.well-known', '/favicon.svg', '/favicon.ico']

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    strictPort: false,
    // 允许通过 PyCharm / 局域网访问
    host: '127.0.0.1',
    proxy: Object.fromEntries(
      proxied.map((p) => [p, { target, changeOrigin: true }]),
    ),
  },
  build: {
    outDir: 'dist',
    // 静态产物可直接丢给后端 / 任意静态服务器托管
    sourcemap: false,
  },
})
