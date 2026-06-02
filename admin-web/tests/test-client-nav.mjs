import { chromium } from 'playwright-core';
const BASE = 'http://localhost:13000';
const wait = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({
    executablePath: '/home/mu-sh/.local/bin/chromium-wrapper',
    headless: true, args: ['--no-sandbox'],
  });
  const page = await browser.newPage();
  
  await page.goto(BASE + '/admin/login', { waitUntil: 'networkidle' });
  await page.fill('input[autocomplete="username"]', 'admin');
  await page.fill('input[autocomplete="current-password"]', 'admin');
  await page.click('button[type="submit"]');
  await page.waitForURL('**/admin', { timeout: 10000 });
  await page.click('nav a[href="/admin/reference/danbooru"]');
  await page.waitForURL('**/danbooru', { timeout: 10000 });
  await wait(1000);
  
  // Submit
  await page.evaluate(() => {
    document.querySelectorAll('button').forEach(b => {
      if (b.textContent && b.textContent.includes('展开')) b.click();
    });
  });
  await wait(300);
  await page.locator('textarea').last().fill('1980643\n1554775\n75220791');
  await page.locator('button').filter({ hasText: '批量导入' }).last().click();
  await wait(2000);
  
  console.log('✅ 提交批导入，processing...');
  
  // Navigate away via CLIENT-SIDE (native Next.js Link)
  await page.locator('nav a[href="/admin/creators"]').last().click();
  await page.waitForURL('**/creators', { timeout: 5000 });
  await wait(1000);
  console.log('✅ 导航到创作者（客户端）');
  
  await page.locator('nav a[href="/admin/subscriptions"]').last().click();
  await page.waitForURL('**/subscriptions', { timeout: 5000 });
  await wait(1000);
  console.log('✅ 导航到订阅（客户端）');
  
  // Wait for job to complete
  await wait(10000);
  
  // Navigate back to Danbooru via CLIENT-SIDE
  await page.locator('nav a[href="/admin/reference/danbooru"]').last().click();
  await page.waitForURL('**/danbooru', { timeout: 5000 });
  await wait(2000);
  
  // Check state
  const state = await page.evaluate(() => ({
    hasProgress: !!document.querySelector('.bg-blue-600'),
    hasResult: !!document.querySelector('.grid.grid-cols-4'),
    resultText: document.querySelector('.grid.grid-cols-4')?.textContent?.trim().substring(0, 50) || 'none',
  }));
  console.log('客户端导航后 Danbooru 状态:', JSON.stringify(state));
  
  if (state.hasResult) console.log('✅ 后台任务完成，结果在客户端导航后可见！');
  else console.log('⚠ 结果未显示（任务可能还在运行中）');
  
  await browser.close();
})().catch(e => { console.error('FAIL:', e.message); process.exit(1); });
