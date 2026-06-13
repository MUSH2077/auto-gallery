/**
 * E2E test: Danbooru batch import → post-import navigation sanity check.
 *
 * Usage:
 *   cd admin-web
 *   PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chromium node tests/danbooru-import-flow.mjs
 */

import { BASE, launchBrowser, wait } from './helpers.mjs';


async function main() {
  const browser = await launchBrowser();

  const errors = [];
  const page = await browser.newPage();

  page.on('console', msg => {
    if (msg.type() === 'error') errors.push('[CONSOLE] ' + msg.text());
  });
  page.on('pageerror', err => errors.push('[PAGE] ' + err.message));

  // ─── Login ───
  console.log('1. 登录...');
  await page.goto(BASE + '/admin/login', { waitUntil: 'networkidle' });
  await page.fill('input[autocomplete="username"]', 'admin');
  await page.fill('input[autocomplete="current-password"]', 'admin');
  await page.click('button[type="submit"]');
  await page.waitForURL('**/admin', { timeout: 10000 });
  console.log('   ✅ 已登录');

  // ─── Navigate to Danbooru ───
  console.log('2. 导航到 Danbooru...');
  await page.click('nav a[href="/admin/reference/danbooru"]');
  await page.waitForURL('**/danbooru', { timeout: 10000 });
  await wait(1000);
  console.log('   ✅ 页面已加载');

  // ─── Expand batch sections ───
  console.log('3. 展开批导入面板...');
  // Use evaluate for reliable clicking (bypasses Playwright strict mode issues)
  await page.evaluate(() => {
    document.querySelectorAll('button').forEach(b => {
      if (b.textContent && b.textContent.includes('展开')) b.click();
    });
  });
  await wait(500);
  const taCount = await page.locator('textarea').count();
  console.log('   ✅ textarea 数量: ' + taCount);

  // ─── Fill and submit batch import ───
  console.log('4. 提交批导入...');
  const ids = '1980643\n1554775\n75220791\n105511715\n76192800\n12474117\n21002119\n8965979\n103514228\n23338848\n14161677\n5271609';
  await page.locator('textarea').last().fill(ids);
  await page.locator('button').filter({ hasText: '批量导入' }).last().click();
  console.log('   ✅ 已提交');

  // ─── Wait for result ───
  console.log('5. 等待完成...');
  let done = false;
  for (let i = 1; i <= 40 && !done; i++) {
    await wait(3000);
    done = await page.evaluate(() => !!document.querySelector('.grid.grid-cols-4'));
    if (done) console.log('   ✅ 完成 (' + (i * 3) + 's)');
  }
  if (!done) console.log('   ⚠️  超时');
  await page.screenshot({ path: '/tmp/e2e-1-import-done.png', fullPage: true });

  // ─── Test post-import navigation ───
  console.log('\n6. 导入后导航测试...');
  const navTests = [
    ['仪表盘', '/admin'],
    ['创作者', '/admin/creators'],
    ['订阅', '/admin/subscriptions'],
    ['任务', '/admin/jobs'],
  ];

  for (const [label, href] of navTests) {
    const link = page.locator('nav a[href="' + href + '"]').last();
    await link.click({ timeout: 5000 }).catch(() => {});
    await wait(800);
    const ok = page.url().includes(href);
    console.log('   ' + (ok ? '✅' : '❌') + ' ' + label + ' → ' + page.url().substring(22));
    if (!ok) {
      errors.push('[NAV] ' + label + ' navigation stuck at ' + page.url());
      // Fallback: hard navigate
      await page.goto(BASE + href, { waitUntil: 'networkidle', timeout: 10000 }).catch(() => {});
    }
  }

  await page.screenshot({ path: '/tmp/e2e-2-final.png', fullPage: true });

  // ─── Report ───
  console.log('\n═══════════════════════════════════');
  console.log('结果: ' + (errors.length === 0 ? '✅ 全部通过' : '❌ ' + errors.length + ' 个错误'));
  if (errors.length > 0) errors.forEach((e, i) => console.log('  ' + (i + 1) + '. ' + e.substring(0, 150)));
  console.log('═══════════════════════════════════');

  await browser.close();
  process.exit(errors.length > 0 ? 1 : 0);
}

main().catch(e => {
  console.error('FATAL:', e.message);
  process.exit(1);
});
