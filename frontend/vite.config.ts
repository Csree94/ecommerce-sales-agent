import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev proxy forwards /api to FastAPI so no CORS setup is needed for local
// development (the backend's CORS_ALLOWED_ORIGINS remains opt-in).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
