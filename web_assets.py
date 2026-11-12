"""Serve one page with content-versioned local assets, including ZIP installs."""
import hashlib
import html
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


_LOCAL_ASSET = re.compile(
    r"""(?P<prefix>\b(?:src|href)\s*=\s*)(?P<quote>["'])(?P<url>/web/[^"']+)(?P=quote)""",
    re.IGNORECASE,
)


def index_html(web_dir):
    """Read current files on each page load; timestamps and Git are not versions."""
    root = Path(web_dir).resolve()
    source = (root / "index.html").read_text(encoding="utf-8")

    def version(match):
        url = urlsplit(html.unescape(match.group("url")))
        path = (root / unquote(url.path[len("/web/"):])).resolve()
        try:
            path.relative_to(root)
            content = path.read_bytes()
        except (ValueError, OSError):
            return match.group(0)
        digest = hashlib.sha256(content).hexdigest()[:16]
        query = [(key, value) for key, value in parse_qsl(url.query, keep_blank_values=True)
                 if key != "v"]
        query.append(("v", digest))
        current = urlunsplit(("", "", url.path, urlencode(query), url.fragment))
        quote = match.group("quote")
        return match.group("prefix") + quote + html.escape(current, quote=True) + quote

    return _LOCAL_ASSET.sub(version, source)
