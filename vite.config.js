const { defineConfig } = require("vite");

const { version } = require("./package.json");

module.exports = defineConfig({
  base: "./",
  // Shown in the sidebar header; bumped by `npm run release`.
  define: {
    __APP_VERSION__: JSON.stringify(version)
  },
  server: {
    host: "127.0.0.1",
    port: 5178,
    strictPort: true
  },
  build: {
    outDir: "dist",
    emptyOutDir: true
  }
});
