/* Exercise the actual cloud dialog controller, including LAN clipboard fallback. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const cloud = require('../web/cloud-connect.js');
const html = fs.readFileSync('web/index.html', 'utf8');
const app = fs.readFileSync('web/app.js', 'utf8');
const controller = app.slice(app.indexOf('let cloudSiteIdOverride'),
  app.indexOf('/* ---------------- 顶部：零算力自动重启设置'));
const code = values => 'AMC1-' + Buffer.from(JSON.stringify(values), 'utf8').toString('base64url');
const payload = {u: 'https://cloud.example.test', t: 'only-a-test-token',
  s: 'site-one', n: '云海 二期', y: 'hydro', f: 'ab'.repeat(32)};

function setup(clipboard, result = {ok: true}) {
  let focused = '';
  const elements = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(match => {
    const classes = new Set(match[1] === 'cloudModal' ? ['hidden'] : []);
    return [match[1], {value: '', type: 'password', textContent: '', disabled: false,
      open: false, handlers: {}, classList: {
        add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x),
        toggle: (x, on) => on ? classes.add(x) : classes.delete(x)},
      addEventListener(name, handler) { this.handlers[name] = handler; },
      setAttribute() {}, focus() { focused = match[1]; }, select() {},
      querySelectorAll() { return []; }}];
  }));
  const calls = [];
  const context = {AntMonitorCloud: cloud, navigator: {clipboard}, myRole: 'admin', _wsStop: true,
    $: id => { assert(elements[id], `Unknown dialog element: ${id}`); return elements[id]; },
    t: key => key, clearTimeout() {}, setTimeout() { return 1; }, console,
    jget: async () => ({site_id: 'existing-id', cloud: {enabled: false, token_set: false,
      site_name: '', site_type: 'air', url: '', tls_fingerprint: ''}}),
    jpost: async (url, body) => {
      calls.push({url, body: JSON.parse(JSON.stringify(body))});
      if (url.endsWith('/test')) return result;
      const {token, ...rest} = body.cloud;
      return {ok: true, cloud: {...rest, token_set: !!token}, site_id: body.cloud.site_id};
    }};
  vm.createContext(context);
  vm.runInContext(controller, context);
  return {context, elements, calls, focused: () => focused,
    run: text => vm.runInContext(text, context)};
}

(async () => {
  assert(!/id="cloudOpen"/.test(html), 'local dialog must not link to the cloud dashboard');
  assert(!controller.includes('$("cloudOpen")'), 'removed navigation must not be restored by status polling');

  const direct = setup({readText: async () => code(payload)});
  await direct.run('openCloud()');
  await direct.elements.cloudPaste.onclick();
  assert.deepEqual(direct.calls.map(x => x.url), ['/api/cloud/test', '/api/settings']);
  assert.equal(direct.calls[1].body.cloud.site_name, payload.n);
  assert.equal(direct.calls[1].body.cloud.site_id, payload.s);
  assert.equal(direct.calls[1].body.cloud.token, payload.t);
  assert.equal(direct.elements.cloudSiteSummary.textContent, payload.n);
  assert.equal(direct.elements.cloudSiteDetails.open, false);
  assert.equal(direct.elements.cloudCode.value, '');
  assert.equal(direct.elements.cloudToken.value, '');

  for (const clipboard of [undefined, {readText: async () => { throw Error('NotAllowedError'); }}]) {
    const lan = setup(clipboard);
    await lan.run('openCloud()');
    await lan.elements.cloudPaste.onclick();
    assert.equal(lan.calls.length, 0);
    assert.equal(lan.focused(), 'cloudCode');
    assert.equal(lan.elements.cloudFeedback.textContent, 'cloud.paste_manual');
    let prevented = false;
    await lan.elements.cloudCode.handlers.paste({clipboardData: {getData: () => code(payload)},
      preventDefault() { prevented = true; }});
    assert(prevented);
    assert.deepEqual(lan.calls.map(x => x.url), ['/api/cloud/test', '/api/settings']);
    await lan.elements.cloudCode.handlers.paste({clipboardData: {getData: () => code(payload)},
      preventDefault() { throw Error('ordinary paste should not be intercepted'); }});
    assert.equal(lan.calls.length, 2, 'paste intent is used only once');
  }

  const manual = setup(undefined);
  await manual.run('openCloud()');
  manual.elements.cloudCode.value = code(payload);
  await manual.elements.cloudCode.handlers.input();
  assert.equal(manual.calls.length, 0);
  await manual.elements.cloudBindingForm.onsubmit({preventDefault() {}});
  assert.equal(manual.calls.length, 2);

  const failed = setup({readText: async () => code(payload)}, {ok: false, message: 'offline'});
  await failed.run('openCloud()');
  await failed.elements.cloudPaste.onclick();
  assert.deepEqual(failed.calls.map(x => x.url), ['/api/cloud/test']);
  assert.equal(failed.elements.cloudCode.value, code(payload));
  assert.equal(failed.elements.cloudPaste.disabled, false);

  const legacy = setup({readText: async () => code({u: payload.u, t: payload.t})});
  await legacy.run('openCloud()');
  await legacy.elements.cloudPaste.onclick();
  assert.equal(legacy.calls.length, 0);
  assert.equal(legacy.elements.cloudSiteDetails.open, true);
  assert.equal(legacy.focused(), 'cloudName');

  let release;
  const late = setup({readText: () => new Promise(resolve => { release = resolve; })});
  await late.run('openCloud()');
  const pending = late.elements.cloudPaste.onclick();
  late.run('closeCloud()');
  release(code(payload));
  await pending;
  assert.equal(late.calls.length, 0, 'closing the dialog cancels a pending clipboard action');

  for (const value of ['', 'not connection information', code({...payload, u: 'https://user:secret@host/'})]) {
    const invalid = setup({readText: async () => value});
    await invalid.run('openCloud()');
    await invalid.elements.cloudPaste.onclick();
    assert.equal(invalid.calls.length, 0);
  }
  console.log('Cloud dialog: clipboard, LAN fallback, manual paste, old codes, failure and cancellation passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
