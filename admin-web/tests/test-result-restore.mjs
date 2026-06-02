import { chromium } from 'playwright-core';
const BASE = 'http://localhost:13000';
const wait = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({
    executablePath: '/home/mu-sh/.local/bin/chromium-wrapper',
    headless: true, args: ['--no-sandbox'],
  });
  const page = await browser.newPage();
  
  // Collect console
  page.on('console', msg => { if (msg.type() === 'error') console.log('[ERR]', msg.text().substring(0,100)); });
  
  await page.goto(BASE + '/admin/login', { waitUntil: 'networkidle' });
  await page.fill('input[autocomplete="username"]', 'admin');
  await page.fill('input[autocomplete="current-password"]', 'admin');
  await page.click('button[type="submit"]');
  await page.waitForURL('**/admin', { timeout: 10000 });
  await page.click('nav a[href="/admin/reference/danbooru"]');
  await page.waitForURL('**/danbooru', { timeout: 10000 });
  await wait(1000);
  
  // Submit just 2 IDs for quick test
  await page.evaluate(() => {
    document.querySelectorAll('button').forEach(b => {
      if (b.textContent && b.textContent.includes('展开')) b.click();
    });
  });
  await wait(300);
  await page.locator('textarea').last().fill('1980643\n1554775');
  await page.locator('button').filter({ hasText: '批量导入' }).last().click();
  await wait(3000);
  
  // Wait for completion on THIS page
  console.log('等待完成...');
  let resultShown = false;
  for (let i = 1; i <= 20; i++) {
    await wait(3000);
    const hasGrid = await page.evaluate(() => !!document.querySelector('.grid.grid-cols-4'));
    if (hasGrid) {
      console.log('✅ 结果在 ' + (i*3) + 's 出现');
      resultShown = true;
      break;
    }
  }
  
  // Read result text
  if (resultShown) {
    const resultText = await page.evaluate(() => {
      const grid = document.querySelector('.grid.grid-cols-4');
      return grid?.textContent?.replace(/\s+/g, ' ').trim().substring(0, 200);
    });
    console.log('结果: ' + resultText);
  } else {
    console.log('⚠ 结果未显示');
    
    // Check bell notification
    await page.locator('button[aria-label="通知"]').click();
    await wait(500);
    const bellText = await page.evaluate(() => {
      const dropdown = document.querySelector('.max-h-\\[480px\\]');
      return dropdown?.textContent?.substring(0, 300) || 'no dropdown';
    });
    console.log('通知面板: ' + bellText);
  }
  
  await browser.close();
})().catch(e => { console.error('FAIL:', e.message); process.exit(1); });
