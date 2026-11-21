/* Optional UI smoke test (requires jsdom: `npm install jsdom`).
   Mounts the real static/index.html + app.js against the running API and
   drives the full flow: query -> receive -> inspect -> reports.
   Run:  node tests/ui_smoke.js            (services must be up)
         NODE_PATH=/path/to/node_modules node tests/ui_smoke.js */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const BASE = process.env.BASE_URL || 'http://127.0.0.1:8000';
const STATIC = path.join(__dirname, '..', 'static');

async function main() {
  const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8')
    .replace('<script src="/vendor/vue.global.prod.js"></script>', '')
    .replace('<script src="/app.js"></script>', '');

  const dom = new JSDOM(html, { url: BASE, runScripts: 'outside-only' });
  const { window } = dom;

  window.fetch = (p, opts) => fetch(p.startsWith('http') ? p : BASE + p, opts);
  window.URL.createObjectURL = () => 'blob:mock';
  window.URL.revokeObjectURL = () => {};
  window.confirm = () => true;

  window.eval(fs.readFileSync(path.join(STATIC, 'vendor/vue.global.prod.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8'));

  const errors = [];
  window.addEventListener('error', (e) => errors.push(e.message));

  const $ = (sel) => window.document.querySelector(sel);
  const $$ = (sel) => [...window.document.querySelectorAll(sel)];
  const text = () => window.document.body.textContent;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const until = async (fn, what, timeout = 10000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      if (fn()) return;
      await sleep(100);
    }
    throw new Error(`timeout waiting for: ${what}`);
  };

  await fetch(BASE + '/api/reset', { method: 'POST' });

  const tabButtons = () => $$('nav.tabs button');
  await until(() => tabButtons().length === 3, 'tabs render');

  $$('button.btn').find((b) => b.textContent.includes('查询演示影像服务')).click();
  await until(() => text().includes('PHANTOM^HEAD^A'), 'studies table');
  console.log('studies rendered OK');

  $('table.grid tbody tr').click();
  await until(() => text().includes('接收此序列'), 'series table');
  console.log('series rendered OK');

  $$('.panel')[1].querySelector('tbody tr').click();
  await until(() => text().includes('来自演示服务的预告'), 'instance preview');
  console.log('instance preview OK');

  $$('button.btn').find((b) => b.textContent.trim() === '接收此序列').click();
  await until(() => text().includes('接收结果'), 'receive result', 20000);
  await until(() => text().includes('已入库'), 'stored badge');
  console.log('receive flow OK');

  tabButtons()[1].click();
  await until(() => $$('.tree .inst').length > 0, 'local tree');
  console.log('local tree instances:', $$('.tree .inst').length);

  $$('.tree .inst')[0].click();
  await until(() => text().includes('实例详情'), 'instance detail');
  await until(() => $$('.fields-group').length >= 4, 'field groups');
  const groups = $$('.fields-group summary').map((e) => e.textContent.trim());
  console.log('field groups:', groups.join(' | '));
  await until(() => text().includes('文件元信息'), 'file meta group');
  console.log('instance detail OK');

  tabButtons()[2].click();
  await until(() => text().includes('引用完整性报告'), 'integrity report');
  await until(() => text().includes('匿名化规则'), 'rules');
  await until(() => text().includes('移除字段'), 'rules lists');
  console.log('reports tab OK');

  if (errors.length) {
    console.log('PAGE ERRORS:', errors);
    process.exit(1);
  }
  console.log('UI SMOKE TEST PASSED');
  process.exit(0);
}

main().catch((e) => { console.error('FAILED:', e.message); process.exit(1); });
