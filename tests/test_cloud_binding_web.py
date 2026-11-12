"""Exercise the same code parser and test/save sequence used by the browser."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def node(script):
    if not NODE:
        pytest.skip("Node.js is needed to execute browser connection logic")
    prefix = "const assert = require('node:assert/strict');\nconst cloud = require('./web/cloud-connect.js');\n"
    result = subprocess.run([NODE, "-e", prefix + script], cwd=ROOT, capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_new_utf8_and_legacy_codes_keep_site_identity():
    node(r"""
    const code = d => 'AMC1-' + Buffer.from(JSON.stringify(d), 'utf8').toString('base64url');
    const base = {u: 'https://cloud.example.test', t: 'test-token', f: 'ab'.repeat(32)};
    for (const name of ['内蒙一场', 'موقع تجريبي', 'Site A']) {
      const got = cloud.parseCode(code({...base, s: 'site-a', n: name, y: 'hydro'}));
      assert.equal(got.n, name); assert.equal(got.y, 'hydro'); assert.equal(got.s, 'site-a');
    }
    for (const sid of ['云海 二期', 'Site A', 'legacy-site', '矿'.repeat(64)]) {
      const got = cloud.parseCode(code({...base, s: sid}));
      assert.equal(got.s, sid); assert.equal(got.n, ''); assert.equal(got.y, '');
    }
    const shared = cloud.parseCode(code(base));
    assert.equal(shared.s, ''); assert.equal(shared.n, '');
    for (const invalid of [{...base, s: '矿'.repeat(65)}, {...base, s: '\n'},
        {...base, s: 'Site\n A'}, {...base, s: ' Site A '}, {...base, s:'Site\x7f A'},
        {...base, n: 1}, {...base, y: 'wrong'}, {...base, t: ''}, {...base, s: {}},
        {...base, u: 'https://cloud .example.test'}, {...base, u: 'https://cloud.example.test/a b'}]) {
      assert.equal(cloud.parseCode(code(invalid)), null);
    }
    assert.equal(cloud.parseCode('AMC1-not-a-code'), null);
    assert.equal(cloud.parseCode(''), null);
    """)


def test_failed_test_never_saves_and_keeps_input():
    node(r"""
    (async () => {
      const value = {enabled:true, url:'https://cloud.example.test', token:'secret',
                     site_name:'Site A', site_type:'air', tls_fingerprint:''};
      const original = JSON.stringify(value), calls = [], phases = [];
      const request = async (url, body) => { calls.push({url, body}); return {ok:false, message:'bad token'}; };
      await assert.rejects(cloud.connect(value, request, phase => phases.push(phase)),
        e => e.step === 'test' && e.message === 'bad token');
      assert.deepEqual(calls.map(x => x.url), ['/api/cloud/test']);
      assert.deepEqual(phases, ['testing']);
      assert.equal(JSON.stringify(value), original);
      await assert.rejects(cloud.connect(value, async () => {throw Error('offline');}),
                           e => e.step === 'test_network');
    })().catch(e => { console.error(e); process.exitCode = 1; });
    """)


def test_success_tests_then_saves_only_cloud_and_distinguishes_save_failure():
    node(r"""
    (async () => {
      const value = {enabled:true, url:'https://cloud.example.test', token:'secret',
                     site_name:'Site A', site_type:'hydro', tls_fingerprint:''};
      const calls = [], phases = [];
      const request = async (url, body) => { calls.push({url, body}); return {ok:true, cloud:{token_set:true}}; };
      const saved = await cloud.connect(value, request, phase => phases.push(phase));
      assert.ok(saved.ok);
      assert.deepEqual(phases, ['testing', 'saving']);
      assert.deepEqual(calls.map(x => x.url), ['/api/cloud/test', '/api/settings']);
      assert.deepEqual(Object.keys(calls[1].body), ['cloud']);
      assert.equal(calls[1].body.cloud.enabled, true);
      assert.equal(calls[1].body.cloud.site_name, 'Site A');
      await assert.rejects(cloud.connect(value, async url => url.endsWith('/test')
        ? {ok:true} : {ok:false, error:'disk full'}), e => e.step === 'save' && e.message === 'disk full');
      await assert.rejects(cloud.connect(value, async url => {
        if (url.endsWith('/test')) return {ok:true}; throw Error('reply lost');
      }), e => e.step === 'save_network');
      assert.equal(cloud.validate({...value, site_name:''}, true), 'site_name');
      assert.equal(cloud.validate({...value, token:''}, false), 'token');
      assert.equal(cloud.validate({...value, token:''}, true), '');
    })().catch(e => { console.error(e); process.exitCode = 1; });
    """)


def test_all_languages_include_new_connection_messages():
    dictionaries = {}
    for lang in ("zh", "en", "ru", "es", "de", "ar"):
        text = (ROOT / "web/i18n" / (lang + ".js")).read_text()
        dictionaries[lang] = json.loads(text[text.index("{"):text.rindex("}") + 1])
    keys = {key for key in dictionaries["zh"] if key.startswith("cloud.")}
    keys.add("header.cloud")
    for lang, dictionary in dictionaries.items():
        assert all(dictionary.get(key) for key in keys), lang
        for key in ("cloud.test_failed", "cloud.save_failed", "cloud.retrying"):
            assert "{error}" in dictionary[key], (lang, key)


def test_actual_dialog_pastes_and_connects_with_lan_fallback():
    if not NODE:
        pytest.skip("Node.js is needed to execute browser connection logic")
    result = subprocess.run([NODE, "tests/cloud_binding_behavior.cjs"], cwd=ROOT,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
