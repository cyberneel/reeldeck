// Touch on the phone layout (timeline.js "touch"), against the running server with every
// write blocked, so any project is safe to point it at:
//   node test_touch.mjs [project-id]
// a swipe over an unselected clip scrolls; a tap selects; a drag on the selected clip edits it.
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
let writes = 0;
await p.route('**/api/**', (r) => r.request().method() === 'GET' ? r.continue() : (writes++, r.abort()));
await p.goto('http://127.0.0.1:8810/?p=' + (process.argv[2] || '')); await p.waitForTimeout(2500);
const cdp = await p.context().newCDPSession(p);
const touch = async (pts) => {
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: pts[0][0], y: pts[0][1] }] });
  for (const [x, y] of pts.slice(1)) { await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y }] }); await p.waitForTimeout(16); }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }); await p.waitForTimeout(300);
};
await p.evaluate(() => document.querySelector('#tlwrap').scrollIntoView());
await p.waitForTimeout(300);
const box = async (sel) => (await p.locator(sel).first().boundingBox());
const state = () => p.evaluate(() => ({ sc: document.querySelector('#tlwrap').scrollLeft,
  sel: document.querySelectorAll('#tl .sel').length,
  left: document.querySelector('.aclip[data-line="1"]')?.style.left }));
const a = await box('.aclip[data-line="1"]'); const cx = a.x + a.width / 2, cy = a.y + a.height / 2;
const s0 = await state();
// 1. swipe left over the unselected voice clip: scrolls, nothing edited
await touch([[cx, cy], ...Array.from({ length: 8 }, (_, i) => [cx - 15 * (i + 1), cy])]);
const s1 = await state(); console.log('swipe', s0, s1);
assert.ok(s1.sc > s0.sc, 'swipe should scroll'); assert.equal(s1.left, s0.left); assert.equal(s1.sel, 0);
// 2. tap: selects it (back to the start so the clip is on screen)
await p.evaluate(() => { document.querySelector('#tlwrap').scrollLeft = 0; }); await p.waitForTimeout(200);
const a2 = await box('.aclip[data-line="1"]');
await touch([[a2.x + a2.width / 2, a2.y + a2.height / 2]]);
const s2 = await state(); console.log('tap', s2); assert.equal(s2.sel, 1);
// 3. drag the selected clip right: it moves (the pause before it grows), the lane doesn't scroll
const a3 = await box('.aclip.sel'); const x3 = a3.x + a3.width / 2, y3 = a3.y + a3.height / 2;
await touch([[x3, y3], ...Array.from({ length: 6 }, (_, i) => [x3 + 8 * (i + 1), y3])]);
const s3 = await state(); console.log('drag', s3);
assert.ok(parseFloat(s3.left) > parseFloat(s2.left), 'selected clip should move'); assert.equal(s3.sc, s2.sc);
await p.waitForTimeout(800); console.log('blocked writes', writes, '(autosave tried; nothing reached the server)');
await b.close(); console.log('touch ok');
