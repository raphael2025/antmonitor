"""HTTP regression for HTML/assets from one current frontend revision."""
import hashlib
import html
import os
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from web_assets import index_html


def refs(source):
    return [html.unescape(m.group(1)) for m in
            re.finditer(r"""(?:src|href)=["'](/web/[^"']+)["']""", source)]


def asset_map(source):
    return {urlsplit(url).path: url for url in refs(source)}


def test_page_versions_all_local_assets_without_changing_load_order(page):
    server, client, web, private_path = page
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    original = refs((web / "index.html").read_text(encoding="utf-8"))
    current = refs(response.text)
    assert current
    assert [urlsplit(url).path for url in current] == [urlsplit(url).path for url in original]
    assert not (web.parent / ".git").exists(), "ZIP/plain-directory deployment needs no Git metadata"
    for url in current:
        version = parse_qs(urlsplit(url).query)["v"]
        asset = client.get(url)
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "no-cache"
        assert version == [hashlib.sha256(asset.content).hexdigest()[:16]]
    assert client.get(private_path).status_code == 401, "public login assets do not make private APIs public"


def test_direct_index_uses_the_same_versioned_html(page):
    _server, client, _web, _private_path = page
    response = client.get("/web/index.html")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert refs(response.text) == refs(client.get("/").text)
    assert client.head("/web/index.html").status_code == 200


def test_normal_reload_bypasses_old_script_and_locale_urls_even_with_same_mtime(page):
    _server, client, web, _private_path = page
    changed = [web / "app.js", web / "i18n" / "zh.js"]
    for path in changed:
        path.write_bytes(path.read_bytes() + b"\n// asset revision A\n")
    old = asset_map(client.get("/").text)
    cached = {url: client.get(url).content for url in old.values()}
    for path in changed:
        stat = path.stat()
        before = path.read_bytes()
        after = before[:-2] + b"B\n"
        assert len(before) == len(after)
        path.write_bytes(after)
        os.utime(str(path), ns=(stat.st_atime_ns, stat.st_mtime_ns))
    response = client.get("/", headers={
        "If-None-Match": '"old-page-etag"',
        "If-Modified-Since": "Wed, 21 Oct 2037 07:28:00 GMT",
    })
    assert response.status_code == 200
    current = asset_map(response.text)
    for name in ("/web/app.js", "/web/i18n/zh.js"):
        assert current[name] != old[name], "ordinary page reload must not reuse the stale cache key"
        assert current[name] not in cached
        assert client.get(current[name]).content.endswith(b"// asset revision B\n")
    for name in set(old) - {"/web/app.js", "/web/i18n/zh.js"}:
        assert current[name] == old[name], "unchanged vendor/CSS assets can retain their cache keys"


def test_helper_preserves_attributes_external_urls_and_existing_query(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "app.js").write_text("window.ready = true;", encoding="utf-8")
    (tmp_path / "outside.js").write_text("not a public asset", encoding="utf-8")
    source = (
        '<script src="/web/app.js?lang=en&amp;v=old#ready" defer data-order="1"></script>'
        '<script src="https://cdn.example.invalid/lib.js"></script>'
        '<a href="/api/health">Health</a>'
        '<script src="/web/../outside.js"></script>'
    )
    (web / "index.html").write_text(source, encoding="utf-8")
    rendered = index_html(web)
    local = refs(rendered)[0]
    parsed = urlsplit(local)
    assert parse_qs(parsed.query)["lang"] == ["en"]
    assert parse_qs(parsed.query)["v"] != ["old"]
    assert parsed.fragment == "ready"
    assert ' defer data-order="1"' in rendered
    assert 'src="https://cdn.example.invalid/lib.js"' in rendered
    assert 'href="/api/health"' in rendered
    assert 'src="/web/../outside.js"' in rendered

@pytest.fixture
def page(monkeypatch, tmp_path):
    import importlib
    monkeypatch.setenv("MINER_CONFIG", str(Path(__file__).with_name("server_config.yaml")))
    server = importlib.import_module("server")
    web = tmp_path / "web"
    shutil.copytree(server.WEB_DIR, str(web))
    monkeypatch.setattr(server, "WEB_DIR", str(web))
    mounted = next(route.app for route in server.app.routes if getattr(route, "path", None) == "/web")
    monkeypatch.setattr(mounted, "directory", str(web))
    monkeypatch.setattr(mounted, "all_directories", [str(web)])
    client = TestClient(server.app, base_url="http://127.0.0.1")
    try:
        yield server, client, web, "/api/settings"
    finally:
        client.close()
