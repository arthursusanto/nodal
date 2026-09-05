import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const proxy = { "/api": "http://127.0.0.1:8000" };

export default defineConfig({
  plugins: [react()],
  server: { proxy },
  // `vite preview` serves the production bundle: same proxy, so the built
  // app can be smoke-tested against the real API before shipping.
  preview: { proxy },
});
