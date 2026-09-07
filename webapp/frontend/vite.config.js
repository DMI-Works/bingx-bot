import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Собранный фронт кладём прямо туда, откуда его будет отдавать FastAPI
  build: {
    outDir: "../backend/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // Пока фронт крутится отдельно (npm run dev), проксируем API на бэкенд
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
