// Per-reel collapse on the Publish tab (publish.js "fold"), against the running server with every
// write blocked:   node test_fold.mjs   (takes ~25 s: it sits through one 20 s refresh)
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
const b = await chromium.launch();
const ctx = await b.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
const p = await ctx.newPage();
let writes = 0; const errs = [];
p.on('pageerror', (e) => errs.push(String(e)));
await ctx.route('**/api/**', (r) => r.request().method() === 'GET' ? r.continue() : (writes++, r.abort()));
const open = async () => { await p.goto('http://127.0.0.1:8810/'); await p.waitForTimeout(1500); await p.locator('#tabs button[data-tab="publish"]').first().click(); await p.waitForSelector('.pub'); await p.waitForTimeout(600); };
await open();
const vis = (sel) => p.locator(sel).first().isVisible();
const groups = await p.$$eval('.pubgroup', (ds) => ds.map((d) => [d.dataset.g, d.open, d.querySelectorAll('.pub').length]));
console.log('groups', JSON.stringify(groups));
// 1. fold the first Ready reel
const first = p.locator('.pubgroup[data-g="Ready"] .pub').first(), pid = await first.getAttribute('data-pid');
await first.locator('.pubFold').click();
assert.equal(await first.evaluate((c) => c.classList.contains('folded')), true);
assert.equal(await first.locator('.pubPhone').isVisible(), false);
assert.equal(await first.locator('[data-k="caption"]').isVisible(), false);
assert.equal(await first.locator('.badge').isVisible(), true);                 // the status stays on the folded row
assert.equal(await first.locator('.pubFold').getAttribute('aria-expanded'), 'false');
assert.deepEqual(await p.evaluate(() => JSON.parse(localStorage.pubFolded)), [pid]);
const h = (await first.boundingBox()).height; console.log('folded height', h); assert.ok(h < 90);
// 2. it survives a reload
await open();
assert.equal(await p.locator(`.pub[data-pid="${pid}"]`).evaluate((c) => c.classList.contains('folded')), true);
// 3. Not ready: open the group, fold one, type in another (unsaved edit) and fold it too, then sit through a 20 s refresh
const nr = p.locator('.pubgroup[data-g^="Not ready"]');
await nr.locator('> summary').click();
const a = nr.locator('.pub').nth(0), c2 = nr.locator('.pub').nth(1), apid = await a.getAttribute('data-pid'), cpid = await c2.getAttribute('data-pid');
await a.locator('.pubFold').click();
await c2.locator('[data-k="caption"]').fill('unsaved words'); await c2.locator('.pubFold').focus(); await c2.locator('.pubFold').click();
const n0 = await p.evaluate(() => performance.getEntriesByType('resource').filter((r) => /api\/publish$/.test(r.name)).length);
await p.waitForTimeout(22000);
const n1 = await p.evaluate(() => performance.getEntriesByType('resource').filter((r) => /api\/publish$/.test(r.name)).length);
console.log('list fetches', n0, '->', n1); assert.ok(n1 > n0, 'no refresh happened');
assert.equal(await nr.evaluate((d) => d.open), true, 'the group closed on refresh');
for (const x of [apid, cpid]) assert.equal(await p.locator(`.pub[data-pid="${x}"]`).evaluate((c) => c.classList.contains('folded')), true);
assert.equal(await p.locator(`.pub[data-pid="${cpid}"] [data-k="caption"]`).inputValue(), 'unsaved words');
// 4. unfold: everything back, storage updated
await p.locator(`.pub[data-pid="${pid}"] .pubFold`).click();
assert.equal(await p.locator(`.pub[data-pid="${pid}"] .pubPhone`).isVisible(), true);
assert.deepEqual((await p.evaluate(() => JSON.parse(localStorage.pubFolded))).sort(), [apid, cpid].sort());
assert.equal(writes, 0); assert.deepEqual(errs, []);
console.log('ok: fold, reload, refresh, unsaved edit, unfold; writes', writes);
await b.close();
