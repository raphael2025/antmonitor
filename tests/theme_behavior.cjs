/* Run the actual browser module in a small DOM harness; no extra npm dependencies. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../web/theme.js'), 'utf8');

function browser({ saved, dark = false, hour = 12, storageError = false, noMedia = false } = {}) {
  const events = {};
  const on = (name, fn) => (events[name] ||= []).push(fn);
  const fire = (name, data = {}) => (events[name] || []).forEach(fn => fn(data));
  const state = { antmonitor_theme: saved };
  const attributes = {};
  const selects = [0, 1].map(() => ({
    value: '', options: ['system', 'time', 'light', 'dark'].map(value => ({ value })),
    setAttribute(name, value) { this[name] = value; },
    addEventListener(name, fn) { this[name] = fn; }
  }));
  const label = {};
  const root = { lang: 'zh-CN', setAttribute(name, value) { attributes[name] = value; } };
  const document = {
    documentElement: root, currentScript: { getAttribute: () => 'antmonitor_theme' },
    readyState: 'loading', hidden: false,
    querySelectorAll: selector => selector === '.theme-select' ? selects : [label],
    addEventListener: on, dispatchEvent: event => fire(event.type, event)
  };
  const media = { matches: dark, addEventListener: (_, fn) => on('media', fn) };
  let plugin;
  const Chart = { instances: {}, register: item => { plugin = item; } };
  const window = { Chart, addEventListener: on, setInterval: fn => on('minute', fn) };
  if (!noMedia) window.matchMedia = () => media;
  class FakeDate extends Date { getHours() { return hour; } }
  vm.runInNewContext(source, {
    window, document, Date: FakeDate,
    localStorage: {
      getItem(key) { if (storageError) throw Error('blocked'); return state[key]; },
      setItem(key, value) { if (storageError) throw Error('blocked'); state[key] = value; }
    },
    CustomEvent: class { constructor(type, data) { this.type = type; this.detail = data.detail; } }
  });
  return {
    window, document, root, state, selects, label, media, attributes, fire, Chart,
    hour(value) { hour = value; },
    init() { fire('DOMContentLoaded'); },
    style(chart) { plugin.beforeUpdate(chart); },
    theme: () => attributes['data-theme']
  };
}

test('system preference is applied before DOM ready and follows OS changes', () => {
  const b = browser({ dark: true });
  assert.equal(b.theme(), 'dark');
  b.media.matches = false; b.fire('media');
  assert.equal(b.theme(), 'light');
});
test('manual selection persists, synchronizes both controls and ignores OS changes', () => {
  const b = browser(); b.init();
  b.selects[0].value = 'dark'; b.selects[0].change();
  assert.equal(b.state.antmonitor_theme, 'dark');
  assert.equal(b.selects[1].value, 'dark');
  b.fire('media'); assert.equal(b.theme(), 'dark');
  const reload = browser({ saved: b.state.antmonitor_theme });
  assert.equal(reload.theme(), 'dark');
});
test('time mode changes at the documented local 07:00 and 19:00 boundaries', () => {
  const b = browser({ saved: 'time', hour: 6, dark: true });
  assert.equal(b.theme(), 'dark');
  b.hour(7); b.fire('minute'); assert.equal(b.theme(), 'light');
  b.hour(18); b.fire('minute'); assert.equal(b.theme(), 'light');
  b.hour(19); b.fire('minute'); assert.equal(b.theme(), 'dark');
  b.hour(9); b.fire('media'); assert.equal(b.theme(), 'dark');
  b.fire('visibilitychange'); assert.equal(b.theme(), 'light');
});
test('invalid saved preferences fall back to system; time fallback works without matchMedia', () => {
  const b = browser({ saved: 'invalid', dark: true });
  assert.equal(b.window.AntTheme.mode, 'system'); assert.equal(b.theme(), 'dark');
  const oldBrowser = browser({ noMedia: true, hour: 22 });
  assert.equal(oldBrowser.theme(), 'dark');
  oldBrowser.hour(8); oldBrowser.fire('minute'); assert.equal(oldBrowser.theme(), 'light');
});
test('blocked storage still allows manual switching', () => {
  const b = browser({ storageError: true }); b.init();
  b.window.AntTheme.setMode('dark'); assert.equal(b.theme(), 'dark');
  b.window.AntTheme.setMode('light'); assert.equal(b.theme(), 'light');
});
test('another tab changing or clearing the preference updates this tab', () => {
  const b = browser(); b.state.antmonitor_theme = 'dark';
  b.fire('storage', { key: 'unrelated' }); assert.equal(b.theme(), 'light');
  b.fire('storage', { key: 'antmonitor_theme' }); assert.equal(b.theme(), 'dark');
  delete b.state.antmonitor_theme; b.fire('storage', { key: null });
  assert.equal(b.theme(), 'light');
});
test('theme controls follow all six languages without changing the preference', () => {
  const b = browser({ saved: 'dark' }); b.init();
  for (const lang of ['zh-CN', 'en', 'ru', 'es', 'de', 'ar']) {
    b.root.lang = lang; b.fire('i18n:change');
    assert.ok(b.label.textContent);
    assert.equal(b.selects[0]['aria-label'], b.label.textContent);
    assert.ok(b.selects[0].options.every(option => option.textContent));
    assert.equal(b.selects[0].value, 'dark');
  }
});
test('existing charts repaint with readable axes and preserve scale limits and data', () => {
  const b = browser({ dark: true }); b.init();
  let updates = 0;
  const data = [{ x: 1, y: 2 }];
  const chart = {
    data, options: {
      scales: { x: { ticks: {}, grid: {}, border: {} }, y: { min: 0, max: 12, ticks: {}, grid: { drawOnChartArea: false }, title: {} } },
      plugins: { legend: { labels: {} }, tooltip: { callbacks: {} } }
    },
    update(mode) { assert.equal(mode, 'none'); updates++; b.style(this); }
  };
  b.Chart.instances.example = chart;
  b.window.AntTheme.setMode('light');
  assert.equal(updates, 1);
  assert.equal(chart.options.scales.x.ticks.color, b.window.AntTheme.palette().tick);
  assert.equal(chart.options.plugins.legend.labels.color, b.window.AntTheme.palette().label);
  assert.equal(chart.options.plugins.tooltip.backgroundColor, '#ffffff');
  assert.equal(chart.options.scales.y.max, 12);
  assert.equal(chart.options.scales.y.grid.drawOnChartArea, false);
  assert.equal(chart.data, data);
});
