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
    const plottedPixels = await page.locator('#dashboard-arc').evaluate(canvas => {
      const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      let count = 0;
      for (let i = 0; i < pixels.length; i += 4) {
        if (pixels[i] > 220 && pixels[i + 1] < 70 && pixels[i + 2] > 60 && pixels[i + 2] < 130 && pixels[i + 3] > 100) count++;
      }
      return count;
    });
    assert.ok(plottedPixels > 50, 'Sparse observations must produce visible markers');
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
    await page.waitForFunction(() => document.querySelectorAll('[data-dataset-row]:not([hidden])').length === 1);
    assert.equal(await page.locator('[data-dataset-row]:visible').count(), 1);
    await page.reload();
    assert.equal(await page.locator('#dataset-search').inputValue(), 'tank/data');
    assert.equal(await page.locator('[data-dataset-row]:visible').count(), 1);
    await page.goto('http://127.0.0.1:8765/drives');
    assert.equal(await page.locator('tbody tr:visible').count(), 25);
    await page.locator('input[type=search]').fill('DEMO-024');
    await page.waitForFunction(() => document.querySelectorAll('tbody tr:not([hidden])').length === 1);
    assert.equal(await page.locator('tbody tr:visible').count(), 1);
    await page.screenshot({
      path: 'test-results/drives-filter.png',
      fullPage: true
    });
    for (const path of ['/operations', '/snapshots', '/forecasts', '/settings/tools', '/servers/1/drive?identity=DEMO-001', '/alerts']) {
      const response = await page.goto('http://127.0.0.1:8765' + path);
      assert.equal(response.status(), 200, path);
    }
    await page.goto('http://127.0.0.1:8765/pools');
    assert.equal(await page.locator('.topology-group:visible').count(), 3);
    for (let i = 0; i < 3; i++) {
      assert.equal(await page.locator(`.pool-pane:visible .topology-section .topology-group[data-vdev-name="raidz2-${i}"] ~ .topology-device`).count(), (3 - i) * 6);
    }
    await page.locator('[data-topology-toggle=collapse]:visible').click();
    assert.equal(await page.locator('.topology-device:visible').count(), 0);
    await page.locator('[data-topology-toggle=expand]:visible').click();
    assert.equal(await page.locator('.topology-device:visible').count(), 18);
    const firstGroup = page.locator('.pool-pane:visible .topology-disclosure').first();
    await firstGroup.click();
    assert.equal(await firstGroup.getAttribute('aria-expanded'), 'false');
    assert.equal(await page.locator('.topology-device:visible').count(), 12);
    await firstGroup.focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('.topology-device:visible').count(), 18);
    const capacity = page.locator('.pool-pane:visible .pool-capacity');
    assert.equal(await capacity.locator('[data-capacity-name]').count(), 23);
    assert.match(await capacity.textContent(), /Checkpoint/);
    assert.match(await capacity.textContent(), /Expandable/);
    assert.match(await capacity.textContent(), /70.8%/);
    const elementStats = page.locator('.pool-pane:visible .topology-table [data-vdev-name="raidz2-0"]');
    assert.match(await elementStats.innerText(), /40.0 TiB/);
    assert.match(await elementStats.innerText(), /70.8%/);

    await capacity.locator('.pool-datasets summary').click();
    assert.match(await capacity.locator('.pool-datasets').innerText(), /tank\/data/);
    await page.screenshot({path: 'test-results/pools-desktop.png', fullPage: true});
    await page.locator('.pool-pane:visible .pool-topology').screenshot({path: 'test-results/topology-desktop.png'});
    assert.equal(await page.locator('.pool-tabs [role=tab]').count(), 2);
    await page.locator('.pool-tabs [role=tab]').nth(1).click();
    assert.equal(await page.locator('.pool-pane:visible').count(), 1);
    assert.match(await page.locator('.pool-pane:visible').innerText(), /family photo.jpg/);
    assert.match(await page.locator('.pool-pane:visible').innerText(), /50.00% done/);
    await page.reload();
    assert.equal(await page.locator('.pool-tabs [aria-selected=true] .pool-tab-server').innerText(), 'Boreas Demo');
    await page.locator('.pool-pane:visible .pool-raw summary').click();
    assert.match(await page.locator('.pool-pane:visible .pool-raw pre').innerText(), /tank\/data:<0xdeadbeef>/);
    await page.screenshot({path: 'test-results/pool-errors-desktop.png', fullPage: true});
    await page.locator('.pool-tabs [aria-selected=true]').focus();
    await page.keyboard.press('Home');
    assert.equal(await page.locator('.pool-tabs [aria-selected=true] .pool-tab-server').innerText(), 'Athena Demo');

    await page.goto('http://127.0.0.1:8765/disk-io?server_id=1&identity=DEMO-000');
    await page.locator('#io-charts .chart-status').filter({hasText: 'Updated'}).last().waitFor();
    assert.equal(await page.locator('#io-charts canvas').count(), 8);
    await page.locator('#io-range').selectOption('0.25');
    await page.locator('#io-charts .chart-status').filter({hasText: 'Updated'}).last().waitFor();
    assert.ok(page.url().includes('hours=0.25'));
    await page.locator('#io-range').selectOption('custom');
    await page.locator('#io-start').fill('2026-01-01T01:00');
    await page.locator('#io-end').fill('2026-01-01T00:00');
    await page.locator('#io-range-form button').click();
    assert.match(await page.locator('#io-range-error').innerText(), /end after/);
    const dates = await page.evaluate(() => {
      const local = d => new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
      return [local(new Date(Date.now()-3600000)), local(new Date())];
    });
    await page.locator('#io-start').fill(dates[0]);
    await page.locator('#io-end').fill(dates[1]);
    await page.locator('#io-range-form button').click();
    await page.locator('#io-charts .chart-status').filter({hasText: 'Updated'}).last().waitFor();
    assert.ok(page.url().includes('start='));
    assert.equal(await page.locator('#io-auto').isDisabled(), true);
    await page.screenshot({path: 'test-results/disk-io-desktop.png', fullPage: true});
    await page.locator('#io-disk').selectOption('DEMO-001');
    await page.waitForURL(/identity=DEMO-001/);
    assert.equal(await page.locator('#io-range').inputValue(), 'custom');
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
    await page.goto('http://127.0.0.1:8765/pools');
    assert.equal(await page.locator('.topology-device:visible').count(), 18);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'Mobile topology overflows');
    const capacityScroll = page.locator('.pool-pane:visible .capacity-scroll').first();
    assert.equal(await capacityScroll.evaluate(el => el.scrollWidth > el.clientWidth), true);
    await capacityScroll.evaluate(el => el.scrollLeft = el.scrollWidth);
    await page.screenshot({path: 'test-results/pools-mobile.png', fullPage: true});
    await page.locator('.pool-pane:visible .pool-topology').screenshot({path: 'test-results/topology-mobile.png'});
    await page.locator('.pool-tabs [role=tab]').nth(1).click();
    assert.equal(await page.locator('.pool-pane:visible').count(), 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'Mobile verbose status overflows');
    await page.screenshot({path: 'test-results/pool-errors-mobile.png', fullPage: true});

    await page.goto('http://127.0.0.1:8765/disk-io?server_id=1&identity=DEMO-000&hours=0.25');
    await page.locator('#io-charts .chart-status').filter({hasText: 'Updated'}).last().waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'Mobile disk I/O overflows');
    await page.screenshot({path: 'test-results/disk-io-mobile.png', fullPage: true});
    await page.setViewportSize({width:1440,height:1000});
    await page.goto('http://127.0.0.1:8765/disk-io?server_id=1&identity=DEMO-000');
    await page.locator('#io-compare').evaluate(el => el.closest('details').open=true);
    await page.locator('#io-compare').selectOption(['DEMO-001','DEMO-002']);
    await page.waitForFunction(() => document.querySelectorAll('#io-comparisons canvas').length===3);
    assert.equal(await page.locator('#io-comparisons .chart-status').filter({hasText:'Updated'}).count(),2);
    assert.equal(await page.locator('#io-comparisons .chart-status').filter({hasText:'No samples'}).count(),1);
    await page.screenshot({path:'test-results/disk-comparison.png',fullPage:true});
    for (const width of [1440,390,320]) {
      await page.setViewportSize({width,height:1000});
      for (const path of ['drive-bays','diagnostics','timeline','settings/database','forecasts','snapshots','operations']) {
        const response=await page.goto('http://127.0.0.1:8765/'+path);
        assert.equal(response.status(),200,path);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${path} overflows at ${width}`);
        await page.screenshot({path:`test-results/${path.replace('/','-')}-${width}.png`,fullPage:true});
      }
    }
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exit(1);
});
