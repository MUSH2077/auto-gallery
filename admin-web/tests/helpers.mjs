import { chromium } from 'playwright-core';

export const BASE = process.env.ADMIN_WEB_BASE_URL || 'http://localhost:13000';
export const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export async function launchBrowser() {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;
  if (!executablePath) {
    console.log('SKIP: PLAYWRIGHT_CHROMIUM_EXECUTABLE is not configured.');
    process.exit(0);
  }
  return chromium.launch({
    executablePath,
    headless: true,
    args: ['--no-sandbox'],
  });
}
