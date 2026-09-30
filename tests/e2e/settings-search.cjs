// Settings search + the Keepa page, in a real browser.
//
//   HAWKSENSE_URL=http://localhost:8799 node tests/e2e/settings-search.cjs
//
// Needs a running `hawksense serve` and Playwright (NODE_PATH=$(npm root -g)).

const { chromium, devices } = require('playwright');
const assert = require('assert');
const BASE = (process.env.HAWKSENSE_URL || 'http://localhost:8799').replace(/\/$/, '');
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
  const page = await (await browser.newContext({ ...devices['iPhone 13'] })).newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto('' + BASE + '/#/settings');
  await page.locator('#settings-search').waitFor();
  await page.waitForFunction(() => document.querySelectorAll('#settings-main .settings-row').length >= 9);
  const shots = process.env.SHOTS;
  const type = async (q) => { await page.fill('#settings-search', q); };
  const titles = () => page.$$eval('#settings-results .settings-row b', (els) => els.map((e) => e.textContent));
  const visible = (sel) => page.$eval(sel, (e) => !e.hidden);

  // 0. the Keepa page is reachable from the list
  assert(await page.locator('#settings-main a[href="#/settings/keepa"]').count(), 'Keepa page in the list');

  // 1. a typed query hides the list and shows matches
  await type('keepa');
  assert.equal(await visible('#settings-results'), true);
  assert.equal(await visible('#settings-main'), false);
  console.log('keepa   ->', await titles());
  assert((await titles()).some((t) => /Keepa/i.test(t)));

  // 2. a field label, several words in any order, a help word
  await type('api key');            console.log('api key ->', await titles());
  await type('token access');       console.log('token   ->', await titles());
  await type('telegram chat');      console.log('telegram->', await titles());
  await type('interval hours');     console.log('interval->', await titles());
  await type('dealtas');            console.log('forward ->', await titles());
  await type('ksp');                console.log('ksp     ->', await titles());
  await type('zzzz');
  assert.match(await page.textContent('#settings-results'), /No settings match/);
  if (shots) await page.screenshot({ path: shots + '/none.png' });

  // 3. clicking a result opens that page; secrets are never shown in results
  await type('keepa');
  await page.locator('#settings-results a').first().click();
  await page.waitForURL(/#\/settings\/keepa/);
  await page.getByText('API key').first().waitFor();
  if (shots) await page.screenshot({ path: shots + '/keepa-page.png' });

  // 4. a store result lands on that store
  await page.goto('' + BASE + '/#/settings');
  await page.waitForSelector('#settings-search');
  await type('newegg');
  await page.locator('#settings-results a[data-store]').first().click();
  await page.waitForURL(/#\/settings\/stores/);
  await page.waitForSelector('#store-pick');
  assert.equal(await page.$eval('#store-pick', (e) => e.value), 'newegg');

  // 5. clear button restores the list
  await page.goto('' + BASE + '/#/settings');
  await page.waitForSelector('#settings-search');
  await type('ebay');
  if (shots) await page.screenshot({ path: shots + '/ebay.png' });
  await page.click('#settings-search-clear');
  assert.equal(await visible('#settings-main'), true);
  assert.equal(await visible('#settings-results'), false);
  assert.equal(await page.inputValue('#settings-search'), '');
  console.log('errors:', errors);
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('OK');
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
