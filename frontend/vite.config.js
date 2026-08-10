import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  // Django serves frontend/dist through STATICFILES_DIRS at STATIC_URL, so the
  // built index.html must reference /static/assets/... — the default '/' emits
  // /assets/... and every asset 404s once Django is serving the page.
  base: '/static/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    // `npm run dev` talks to the Django dev server for data.
    proxy: {
      '/api': 'http://localhost:8000',
      '/media': 'http://localhost:8000',
    },
  },
  test: {
    environment: 'jsdom',
  },
})
