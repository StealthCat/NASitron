/* npm install --no-save playwright@1.62.1; npx playwright install chromium */
const {
  chromium
} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
(async () => {
  const browser = await chromium.launch({
    headless: true
  });
  const page = await browser.newPage({
    viewport: {
      width: 1440,
      height: 1000
    }
  });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  fs.mkdirSync('test-results', {
    recursive: true
  });
  try {
    await page.goto('http://127.0.0.1:8765/login');
    await page.locator('[name=username]').fill(process.env.NASITRON_WEB_USERNAME);
    await page.locator('[name=password]').fill(process.env.NASITRON_WEB_PASSWORD);
    await Promise.all([page.waitForURL('http://127.0.0.1:8765/'), page.locator('button[type=submit]').click()]);
    await page.locator('.chart-status').filter({
      hasText: 'Updated'
    }).waitFor();
    await page.screenshot({
      path: 'test-results/dashboard-desktop.png',
      fullPage: true
    });
    assert.equal(await page.locator('#arc-server').inputValue(), '1');
    await page.locator('.chart-controls select').selectOption('6');
    await page.locator('.chart-status').filter({
      hasText: 'Updated'
    }).waitFor();
    await page.goto('http://127.0.0.1:8765/servers/1');
    await page.locator('[data-page-tab=drives]').click();
    assert.equal(await page.locator('[data-page-pane=drives]').isVisible(), true);
    assert.equal(await page.locator('[data-page-pane=pools]').isVisible(), false);
    await page.locator('[data-page-tab=performance]').click();
    await page.locator('.chart-status').filter({
      hasText: 'Updated'
    }).first().waitFor();
    await page.screenshot({
      path: 'test-results/server-performance.png',
      fullPage: true
    });
    await page.goto('http://127.0.0.1:8765/datasets');
    await page.locator('#dataset-search').fill('tank/data');
    assert.equal(await page.locator('[data-dataset-row]:visible').count(), 1);
    await page.reload();
    assert.equal(await page.locator('#dataset-search').inputValue(), 'tank/data');
    assert.equal(await page.locator('[data-dataset-row]:visible').count(), 1);
    await page.goto('http://127.0.0.1:8765/drives');
    assert.equal(await page.locator('tbody tr:visible').count(), 25);
    await page.locator('input[type=search]').fill('DEMO-024');
    assert.equal(await page.locator('tbody tr:visible').count(), 1);
    await page.screenshot({
      path: 'test-results/drives-filter.png',
      fullPage: true
    });
    for (const path of ['/operations', '/snapshots', '/forecasts', '/settings/tools', '/servers/1/drive?identity=DEMO-001', '/alerts']) {
      const response = await page.goto('http://127.0.0.1:8765' + path);
      assert.equal(response.status(), 200, path);
    }
    await page.setViewportSize({
      width: 390,
      height: 844
    });
    await page.goto('http://127.0.0.1:8765/');
    await page.locator('.chart-status').filter({
      hasText: 'Updated'
    }).waitFor();
    await page.screenshot({
      path: 'test-results/dashboard-mobile.png',
      fullPage: true
    });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'Mobile page overflows');
    await page.locator('#nav-toggle').click();
    assert.equal(await page.locator('#nav-toggle').getAttribute('aria-expanded'), 'true');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#nav-toggle').getAttribute('aria-expanded'), 'false');
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exit(1);
});
