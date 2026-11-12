import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig, loadEnv } from "vite"

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "")

  return {
    server: {
      // Listen on all interfaces so the dev server is reachable over Tailscale.
      host: true,
      // Vite blocks unknown Host headers; allow Tailscale MagicDNS names.
      allowedHosts: [".ts.net"],
      // Keep browser sessions first-party by proxying relative API requests.
      proxy: {
        "/api": {
          target: env.VITE_API_PROXY_TARGET ?? "http://localhost:8000",
        },
      },
    },
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
  }
})
