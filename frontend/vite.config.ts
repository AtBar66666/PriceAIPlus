import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Tauri 打包：不要让 Vite 监听 Rust 构建产物（src-tauri/target 会在编译时被占用，
  // 触发 EBUSY 崩溃）；同时保留 Tauri 编译报错输出。
  clearScreen: false,
  server: {
    host: true,
    port: 5173,
    strictPort: true,
    watch: {
      ignored: ['**/src-tauri/**'],
    },
  },
})
