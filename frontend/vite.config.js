import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * Vite 配置文件
 * ---------------------------------------------------------------------------
 * Vite 是前端开发服务器 + 打包工具，作用相当于 Java 那边的 Maven：
 *   - npm run dev   启动开发服务器（改代码浏览器自动刷新，秒级）
 *   - npm run build 把代码打包压缩，产物丢到 dist/，交给 Nginx 部署
 * ---------------------------------------------------------------------------
 */
export default defineConfig({
  // 插件：让 Vite 认识 .vue 文件（把 <template>/<script>/<style> 编译成 JS）
  plugins: [vue()],

  server: {
    port: 5173,
    open: true, // 启动后自动打开浏览器
    host: '127.0.0.1',

    /**
     * 代理（proxy）—— 本项目最重要的一段配置
     *
     * 前端代码里写 fetch('/api/health')，浏览器请求的是 http://127.0.0.1:5173/api/health，
     * 这个地址在 5173 上不存在，正常会 404。Vite 看到 /api 开头，就偷偷转发给 8080，
     * 再把结果原样返回给浏览器。
     *
     * 好处：浏览器全程以为自己在访问同一个源，CORS 跨域问题直接消失。
     * 上线后由 Nginx 做同样的转发，前端代码一行都不用改。
     */
    proxy: {
      '/api': {
        // 默认转到 8080（IDEA 里跑的后端）。
        // 想临时指向别的端口（比如另开一个实例做对比测试），
        // 启动前设环境变量 VITE_PROXY_TARGET=http://localhost:8081
        target: process.env.VITE_PROXY_TARGET || 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },

  build: {
    // Cesium 本身体积很大（压缩后仍有数 MB），默认 500KB 的告警会一直刷屏，调高一点
    chunkSizeWarningLimit: 5000,
    // 打包时不做 sourcemap，产物小一些；需要调试线上问题时改成 true
    sourcemap: false,
  },
})
