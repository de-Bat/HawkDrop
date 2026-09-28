// End-to-end check of the web client, including full offline use.
//
//   HAWKSENSE_URL=http://localhost:8799 node tests/e2e/offline.cjs
//
// Needs a running `hawksense serve` with an empty database and the Playwright
// package (NODE_PATH=$(npm root -g) if installed globally).
const { chromium, devices } = require('playwright');
const assert = require('assert');

const BASE = (process.env.HAWKSENSE_URL || 'http://localhost:8799').replace(/\/$/, '');
const TOKEN = process.env.HAWKSENSE_TOKEN || '';
const SHOTS = process.env.SHOTS_DIR;

async function api(method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  if (TOKEN) headers['X-HawkSense-Token'] = TOKEN;
  const res = await fetch(BASE + path, { method, headers, body: body && JSON.stringify(body) });
  return res.json();
}

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
  const context = await browser.newContext({ ...devices['iPhone 13'], serviceWorkers: 'allow' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => { if (m.type() === 'error' && !/ERR_INTERNET_DISCONNECTED/.test(m.text())) errors.push(m.text()); });
  const shot = async (name) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true }); };
  const step = (msg) => console.log(`• ${msg}`);

  // 1. first visit online: empty state, then demo data
  await page.goto(BASE + '/' + (TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ''));
  await page.getByText('Track your first item').waitFor();
  await shot('01-empty');
  await page.getByRole('button', { name: 'Load demo' }).click();
  await page.locator('.hero').waitFor();
  await page.getByText('Synced').waitFor();
  step('online: demo item loaded and advice rendered');
  await shot('02-item');

  // 2. make sure the service worker controls the page (first load isn't controlled until claim)
  await page.evaluate(() => navigator.serviceWorker.ready);
  await page.waitForFunction(() => !!navigator.serviceWorker.controller);
  step('service worker active');

  // 3. go offline and reload: app shell + data must come from the device
  await context.setOffline(true);
  await page.reload();
  await page.goto(BASE + '/#/');
  await page.locator('.item-card').first().waitFor();
  assert.match(await page.locator('.item-card').first().innerText(), /Sony WH-1000XM5/);
  step('offline reload: item list rendered from the offline copy');
  await page.locator('.item-card').first().click();
  await page.locator('.hero').waitFor();
  assert.ok(await page.locator('.chart svg').count(), 'chart renders offline');
  assert.ok(await page.getByRole('button', { name: /Check/ }).isDisabled(), 'check needs a connection');
  step('offline: item page with advice + chart works, "Check" disabled');

  // 4. log a price offline
  await page.getByRole('button', { name: /Log price/ }).click();
  await page.locator('#price-store').selectOption({ label: 'KSP' });
  await page.fill('input[name=price]', '1199');
  await page.getByRole('button', { name: 'Save price' }).click();
  await page.getByText('waiting to sync').first().waitFor();
  await page.locator('.pill', { hasText: '1 pending' }).waitFor();
  step('offline: price queued (1 pending)');
  await shot('03-offline-pending');

  // 5. create a brand-new item offline and log a price on it
  await page.goto(BASE + '/#/add');
  await page.fill('input[name=name]', 'AirPods Pro 3');
  await page.fill('input[name=target_price]', '850');
  await page.getByRole('button', { name: 'Start tracking' }).click();
  await page.getByText('Advice pending').waitFor();
  assert.match(page.url(), /#\/item\/tmp-/);
  await page.getByRole('button', { name: /Log price/ }).click();
  await page.locator('#price-store').selectOption({ label: '🇺🇸 Amazon.com' });
  assert.equal(await page.locator('#price-cur').inputValue(), 'USD');
  await page.fill('input[name=price]', '229');
  await page.getByRole('button', { name: 'Save price' }).click();
  await page.locator('.pill', { hasText: '3 pending' }).waitFor();
  step('offline: new item + its price queued (3 pending)');

  // 6. survive an app restart while offline
  await page.reload();
  await page.locator('.pill', { hasText: '3 pending' }).waitFor();
  step('offline: queue survives a reload');

  // 7. back online -> everything syncs, temp id is replaced by the real one
  await context.setOffline(false);
  await page.locator('.pill', { hasText: 'Synced' }).waitFor({ timeout: 30000 });
  await page.waitForFunction(() => /#\/item\/\d+$/.test(location.hash));
  await page.locator('.hero').waitFor();
  step(`online: synced, temp item now ${await page.evaluate(() => location.hash)}`);
  await shot('04-synced');

  const snap = await api('GET', '/api/snapshot');
  const airpods = snap.items.find((i) => i.name === 'AirPods Pro 3');
  assert.ok(airpods, 'item created on server');
  assert.equal(airpods.target_price, 850);
  assert.equal(airpods.quotes.length, 1);
  assert.equal(airpods.quotes[0].price, 229);
  const demo = snap.items.find((i) => i.name.startsWith('Sony'));
  const ksp = demo.quotes.find((q) => q.store === 'KSP');
  assert.equal(ksp.price, 1199);
  step('server has both offline prices and the new item');

  // 8. replaying the same queued entry must not duplicate it
  const before = demo.history.length;
  await page.reload();
  await page.locator('.pill', { hasText: 'Synced' }).waitFor();
  assert.equal((await api('GET', `/api/items/${demo.id}`)).history.length, before);

  // 9. other screens
  await page.goto(BASE + '/#/events');
  await page.getByText('Black Friday').first().waitFor();
  await shot('05-events');
  await page.goto(BASE + '/#/settings');
  await page.getByText('Package forwarders').waitFor();
  await shot('06-settings');
  await page.goto(BASE + '/#/settings/device');
  await page.getByText('Everything is synced.').waitFor();
  await page.goto(BASE + '/#/');
  await shot('07-list');
  step('events + settings render');

  // 10. set up a forwarder address offline; once synced, stores abroad are priced through it too
  await context.setOffline(true);
  await page.goto(BASE + '/#/settings/forwarders');
  await page.getByRole('button', { name: /Add a forwarder address/ }).click();
  await page.locator('#fwd-service').selectOption('dealtas');
  await page.fill('textarea[name=address]', 'Test User\n16 Example Rd #IL123\nNew Castle, DE 19720');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await page.locator('#status .pill', { hasText: '1 pending' }).waitFor();
  await page.getByText('New Castle, DE 19720').waitFor();
  step('offline: forwarder address queued');
  await context.setOffline(false);
  await page.locator('#status .pill', { hasText: 'Synced' }).waitFor({ timeout: 30000 });
  const fwd = await api('GET', '/api/forwarders');
  assert.equal(fwd.accounts.length, 1);
  assert.equal(fwd.accounts[0].sales_tax_source, 'DE address');
  await page.goto(BASE + `/#/item/${demo.id}`);
  const amazonQuote = page.locator('.quote', { hasText: 'Amazon.com' }).first();
  await amazonQuote.locator('summary').first().click();
  await amazonQuote.getByText('Other ways to get it').waitFor();
  await shot('08-routes');
  step('online: forwarder synced, item shows the other routes');

  // every forwarder that could ship this item shows up, not just the one you've set up
  await amazonQuote.getByText(/Other ways to get it \(\d+\)/).click();
  const suggestion = amazonQuote.locator('.routes li', { hasText: 'not set up' }).first();
  await suggestion.locator('summary').click();
  await suggestion.getByRole('button', { name: 'Set up this forwarder' }).click();
  await page.getByRole('heading', { name: /^Set up / }).waitFor();
  await page.getByRole('button', { name: 'Close' }).click();
  step('online: capable-but-unset-up forwarders are offered too');
  await page.getByText('Weight & size').waitFor();

  // 11. change a customs rule and notification preferences offline, then sync
  await context.setOffline(true);
  await page.goto(BASE + '/#/settings/rates');
  await page.locator('input[name="rule:destination.IL.vat_exempt_usd"]').fill('150');
  await page.getByRole('button', { name: 'Save rules' }).click();
  await page.goto(BASE + '/#/settings/notifications');
  await page.locator('input[name="sub:price_drop:telegram"]').check();
  await page.fill('input[name=price_drop_pct]', '7');
  await page.getByRole('button', { name: 'Save notification settings' }).click();
  await page.locator('#status .pill', { hasText: '2 pending' }).waitFor();
  step('offline: rule change + notification settings queued');
  await context.setOffline(false);
  await page.locator('#status .pill', { hasText: 'Synced' }).waitFor({ timeout: 30000 });
  const rules = await api('GET', '/api/rules');
  const exempt = rules.values.find((v) => v.path === 'destination.IL.vat_exempt_usd');
  assert.deepEqual([exempt.value, exempt.from], [150, 'manual']);
  const prefs = await api('GET', '/api/notifications');
  assert.deepEqual(prefs.subscriptions.price_drop, ['inbox', 'telegram']);
  assert.equal(prefs.settings.price_drop_pct, 7);
  await shot('09-settings-rules');
  step('online: rule and notification settings saved on the server');

  // 12. a new notification shows on the bell; opening the inbox marks it read
  await api('POST', '/api/notify/test', { channel: 'inbox' });
  await page.locator('#status .pill').click();
  await page.locator('#bell .count').waitFor();
  await page.locator('#bell').click();
  // the inbox entry itself: a "🔔 Test notification" toast may be on screen at the same time
  await page.locator('.note-item').getByText('Test notification').waitFor();
  await shot('10-inbox');
  await page.waitForFunction(async () => (await (await fetch('api/notifications', { headers: { 'X-HawkSense-Token': localStorage.getItem('hawkdrop.token') || '' } })).json()).unread === 0, null, { timeout: 15000 });
  step('inbox: notification shown and marked read');

  // 13. configure the server from the app: channel secrets (online only), schedule + store policy (offline ok)
  await page.goto(BASE + '/#/settings/notifications');
  await page.locator('details.fold', { hasText: 'Telegram' }).locator('summary').click();
  const tg = page.locator('form.settings-form[data-section=telegram]');
  await tg.locator('input[name="set:notify.telegram.bot_token"]').fill('123:TESTTOKEN');
  await tg.locator('input[name="set:notify.telegram.chat_id"]').fill('42');
  await tg.getByRole('button', { name: 'Save' }).click();
  await page.getByText('Settings saved').first().waitFor();
  let sv = await api('GET', '/api/settings');
  assert.ok(sv.sections.find((x) => x.key === 'telegram').configured, 'telegram configured from the app');
  assert.ok(!JSON.stringify(sv).includes('TESTTOKEN'), 'secrets are never sent back');
  await tg.locator('input[name="set:notify.telegram.bot_token"]').waitFor();
  assert.match(await page.locator('form.settings-form[data-section=telegram] input[name="set:notify.telegram.bot_token"]').getAttribute('placeholder'), /saved/);
  step('online: Telegram set up from the app; token not sent back');

  await context.setOffline(true);
  await page.goto(BASE + '/#/settings/checks');
  await page.locator('input[name="set:schedule.check_every"]').fill('6');
  await page.locator('form.settings-form[data-section=checks]').getByRole('button', { name: 'Save' }).click();
  await page.goto(BASE + '/#/settings/stores');
  await page.locator('#store-pick').selectOption('amazon_us');
  await page.locator('input[name="store:shipping_flat"]').fill('15');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await page.goto(BASE + '/#/settings/notifications');
  await page.locator('details.fold', { hasText: 'WhatsApp' }).locator('summary').click();
  await page.locator('input[name="set:notify.whatsapp.apikey"]').fill('k');
  await page.locator('form.settings-form[data-section=whatsapp]').getByRole('button', { name: 'Save' }).click();
  await page.getByText('Connect to your HawkSense server to save passwords').waitFor();
  await page.locator('#status .pill', { hasText: '2 pending' }).waitFor();
  step('offline: schedule + store shipping queued; secrets refused while offline');
  await context.setOffline(false);
  await page.locator('#status .pill', { hasText: 'Synced' }).waitFor({ timeout: 30000 });
  sv = await api('GET', '/api/settings');
  const every = sv.sections.find((x) => x.key === 'checks').fields.find((f) => f.path === 'schedule.check_every');
  assert.deepEqual([every.value, every.source], [6, 'app']);
  assert.equal(sv.stores.find((x) => x.key === 'amazon_us').overrides.shipping_flat, 15);
  await page.goto(BASE + '/#/settings');
  await page.getByText('Prices every 6 h').waitFor();
  await shot('11-settings-index');
  step('online: settings saved on the server and shown in the index');

  assert.deepEqual(errors, [], `page errors: ${errors.join('\n')}`);
  console.log('\nALL E2E CHECKS PASSED');
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
