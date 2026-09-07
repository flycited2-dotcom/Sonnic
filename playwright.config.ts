import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  use: { baseURL: "http://127.0.0.1:3000", trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [{ name: "desktop", use: { ...devices["Desktop Chrome"], channel: "chrome" } },
    { name: "mobile", use: { ...devices["iPhone 13"], defaultBrowserType: "chromium", channel: "chrome" } }],
  webServer: [
    { command: process.platform === "win32" ? ".venv\\Scripts\\python.exe -m uvicorn apps.api.sonnic.main:app --host 127.0.0.1 --port 8000" : ".venv/bin/python -m uvicorn apps.api.sonnic.main:app --host 127.0.0.1 --port 8000", url: "http://127.0.0.1:8000/health/ready", reuseExistingServer: !process.env.CI, env: { SONNIC_DB_PATH: "data/e2e.sqlite3", AI_MODE: "demo" } },
    { command: "npm run start", url: "http://127.0.0.1:3000", reuseExistingServer: !process.env.CI, timeout: 60000 },
  ],
});
