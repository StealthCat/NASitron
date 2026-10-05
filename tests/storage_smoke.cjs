/* CI-only browser checks against the synthetic UI fixture. No remote actions. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
(async () => {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage();
  await page.emulateMedia({reducedMotion:'reduce'});
  const errors=[];page.on('pageerror', e=>errors.push(e.message));
  fs.mkdirSync('test-results',{recursive:true});
  try {
    await page.goto('http://127.0.0.1:8765/login');
    await page.locator('[name=username]').fill(process.env.NASITRON_WEB_USERNAME || 'ci-admin');
    await page.locator('[name=password]').fill(process.env.NASITRON_WEB_PASSWORD || 'ci-password-strong');
    await Promise.all([page.waitForURL('http://127.0.0.1:8765/'),page.locator('button[type=submit]').click()]);
    for(const width of [1440,390]) {
      await page.setViewportSize({width,height:900});
      for(const tab of ['overview','planner','datasets','snapshots','jobs','events','host']) {
        const response=await page.goto(`http://127.0.0.1:8765/servers/1/storage?tab=${tab}`);
        assert.equal(response.status(),200);
        assert.equal(await page.locator('.storage-tabs [aria-current=page]').count(),1);
        if(tab==='datasets') {
          await page.locator('#storage-action-form [name=action]').selectOption('dataset-inherit');
          assert.equal(await page.locator('#storage-value').isVisible(),false);
        }
        if(tab==='snapshots') {
          await page.locator('[data-storage-filter]').fill('missing');
          assert.equal(await page.locator('#snapshot-table tbody tr:visible').count(),0);
        }
        if(tab==='jobs') {
          await page.locator('#storage-policy-form [name=kind]').selectOption('replication');
          assert.equal(await page.locator('[name=destination]').isVisible(),true);
          assert.equal(await page.locator('[name=disk]').isVisible(),false);
        }
        await page.evaluate(() => window.scrollTo({top:0,left:0,behavior:'instant'}));
    await page.screenshot({path:`test-results/storage-${tab}-${width}.png`,fullPage:true});
        assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth), `${tab} overflows at ${width}`);
      }
    }
    assert.deepEqual(errors,[]);
    console.log('Storage workspace desktop/mobile smoke checks passed');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
