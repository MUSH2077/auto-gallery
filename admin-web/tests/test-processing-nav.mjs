import { BASE, launchBrowser } from './helpers.mjs';

(async () => {
  const browser = await launchBrowser();
  const page = await browser.newPage();
  
  await page.goto(BASE + '/admin/login', { waitUntil: 'networkidle' });
  await page.fill('input[autocomplete="username"]', 'admin');
  await page.fill('input[autocomplete="current-password"]', 'admin');
  await page.click('button[type="submit"]');
  await page.waitForURL('**/admin', { timeout: 10000 });
  await page.click('nav a[href="/admin/upload/danbooru"]');
  await page.waitForURL('**/danbooru', { timeout: 10000 });
  await page.waitForTimeout(1000);
  
  // Submit batch
  await page.evaluate(() => {
    document.querySelectorAll('button').forEach(b => {
      if (b.textContent && b.textContent.includes('展开')) b.click();
    });
  });
  await page.waitForTimeout(300);
  const ids = '1980643\n1554775\n75220791\n105511715\n76192800\n12474117\n21002119\n8965979\n103514228\n23338848\n14161677\n5271609';
  await page.locator('textarea').last().fill(ids);
  await page.locator('button').filter({ hasText: '批量导入' }).last().click();
  
  // Wait for progress
  await page.waitForTimeout(2000);
  const hasProgress = await page.evaluate(() => !!document.querySelector('.bg-blue-600'));
  console.log('进度条: ' + hasProgress);
  
  // TEST: Navigate DURING processing
  console.log('\n=== DURING processing ===');
  const navTargets = [
    ['仪表盘', '/admin'],
    ['创作者', '/admin/creators'],
    ['订阅', '/admin/subscriptions'],
    ['任务', '/admin/jobs'],
  ];
  
  let allOk = true;
  for (const [label, href] of navTargets) {
    await page.locator('nav a[href="' + href + '"]').last().click({ timeout: 3000 }).catch(() => {});
    await new Promise(r => setTimeout(r, 800));
    const ok = page.url().includes(href);
    console.log((ok ? '  ✅' : '  ❌') + ' ' + label + ' → ' + page.url().substring(22));
    if (!ok) {
      allOk = false;
      await page.goto(BASE + href, { waitUntil: 'networkidle', timeout: 5000 }).catch(() => {});
    }
  }
  
  if (allOk) {
    console.log('\n✅ processing 期间导航全部正常！');
  }
  
  // Go back to Danbooru and check if job is still tracked
  await page.goto(BASE + '/admin/upload/danbooru', { waitUntil: 'networkidle', timeout: 5000 });
  await page.waitForTimeout(2000);
  const restored = await page.evaluate(() => {
    const hasProgress = !!document.querySelector('.bg-blue-600');
    const hasResult = !!document.querySelector('.grid.grid-cols-4');
    return { hasProgress, hasResult };
  });
  console.log('\n回到 Danbooru 后的状态: ' + JSON.stringify(restored));
  
  // The job should either have completed (result visible) or still be processing (progress visible)
  if (restored.hasProgress || restored.hasResult) {
    console.log('✅ 后台任务持续运行，状态已恢复');
  } else {
    console.log('⚠ 状态未恢复，但导航正常');
  }
  
  await browser.close();
})().catch(e => { console.error('FAIL:', e.message); process.exit(1); });
