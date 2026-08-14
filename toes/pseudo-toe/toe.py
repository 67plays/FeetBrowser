"""Pseudo-Toe — a pseudo-site hub for scrolling GitHub repo sites.

Pseudo-Toe turns any GitHub repo link into a lightweight site rendered by
FeetBrowser itself. Nothing is hosted: every page is built on the fly from
the GitHub API over the browser's own HTTPS stack, so a repo becomes a
scrollable hub of its README and file tree with no heavy page, no ads, and
no JavaScript.

Pseudo-site pages (served by the handle hook):

    gh://hub                                   the hub: featured repos,
                                               jump-to-repo box, search
    gh://browse/<owner>/<repo>                 one repo as a site
    gh://browse/<owner>/<repo>/<path>          a subdirectory or a file
    gh://search/?q=<term>                      GitHub repo search results
    gh://user/<name>                           every public repo of a user

When "intercept github.com" is enabled (default), pasting a normal
https://github.com/<owner>/<repo> link renders the pseudo-site instead of
the full GitHub page. Turn it off in the ToeHub config to only use gh://.
"""

import base64
import json
import os
import re
import sys
import time
import urllib.parse

from feetbrowser.net import URL
from feetbrowser.toes import ButtonDef, ConfigOption

SCHEME = "gh"
API = "https://api.github.com"

CACHE_TTL = 300.0
CACHE_MAX = 200

DEFAULT_FEATURED = (
    "xplosivex/feetbrowser-toes,"
    "JuiceyDew/FeetBrowser,"
    "python/cpython,"
    "torvalds/linux,"
    "vinta/awesome-python,"
    "tldr-pages/tldr,"
    "sindresorhus/awesome,"
    "github/gitignore"
)

STYLE = """
  body { font-family: Courier; margin: 24px; background: #fdf6e3; color: #222; }
  h1 { color: #0d6a2f; letter-spacing: 2px; }
  h2 { color: #444; margin-top: 20px; }
  a { color: #0a66c2; }
  .dim { color: #888; }
  .k { color: #0d6a2f; }
  .box { border: 1px solid #bbb; background: #fff; padding: 6px 10px; margin: 4px 0; }
  .file { border-left: 3px solid #0a66c2; }
  .dir { border-left: 3px solid #0d6a2f; }
"""

_CACHE = {}


def activate(ctx):
    ctx.define_config(
        ConfigOption(
            "intercept_github", "Intercept github.com repo links", "bool",
            default=True,
            help="Render github.com/<owner>/<repo> links as gh:// "
                 "pseudo-sites instead of fetching GitHub."),
        ConfigOption(
            "featured", "Featured repos on the hub", "str",
            default=DEFAULT_FEATURED,
            help="Comma-separated owner/repo list shown on gh://hub."),
        ConfigOption(
            "max_files", "Max files listed per directory", "int",
            default=60,
            help="Cap on the file listing so huge repos stay fast."),
    )
    ctx.on("handle", lambda url, tab: handle(ctx, url, tab))
    ctx.on("buttons", lambda: [ButtonDef("pseudo-toe", "GH")])
    ctx.on("on_click",
           lambda btn: ctx.open("gh://hub") if btn == "pseudo-toe" else None)


# -- routing ---------------------------------------------------------------


def handle(ctx, url, tab):
    if url.scheme == SCHEME:
        return route_gh(ctx, url)
    if ctx.config_value("intercept_github") and _is_repo_link(url):
        return route_gh(ctx, URL(_repo_link_to_gh(url)))
    return None


def route_gh(ctx, url):
    host = url.host or ""
    path = url.path or "/"
    if "?" in host:
        host, _, q = host.partition("?")
        path = path.rstrip("/") + "/?" + q
    if host in ("", "hub"):
        return {}, hub_page(ctx), "text/html"
    if host in ("browse", "repo"):
        q = _query(path)
        if "owner_repo" in q:
            path = "/" + _owner_repo(q["owner_repo"][0])
        return {}, browse_page(ctx, path), "text/html"
    if host == "search":
        return {}, search_page(ctx, path), "text/html"
    if host == "user":
        return {}, user_page(ctx, path), "text/html"
    return {}, hub_page(ctx), "text/html"


def _is_repo_link(url):
    if url.scheme != "https" or url.host != "github.com":
        return False
    parts = [p for p in url.path.split("/") if p]
    if len(parts) == 2:
        return True
    return len(parts) > 2 and parts[2] in ("tree", "blob")


def _repo_link_to_gh(url):
    parts = [p for p in url.path.split("/") if p]
    owner, repo = parts[0], parts[1]
    target = f"{SCHEME}://browse/{owner}/{repo}"
    if len(parts) > 4:
        target += "/" + "/".join(parts[4:])
    return target


def _owner_repo(value):
    value = value.strip()
    if "github.com" in value:
        m = re.search(r"github\.com/([^/]+)/([^/?#]+)", value)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    parts = [p for p in value.split("/") if p]
    if len(parts) >= 2 and "." not in parts[0]:
        return "/".join(parts[:2])
    return value.strip("/")


# -- GitHub API ------------------------------------------------------------


def _gh_get(ctx, api_path):
    url = API + api_path
    now = time.time()
    hit = _CACHE.get(url)
    if hit is not None and now - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        _h, body, _c = URL(url).request()
        data = json.loads(body)
    except Exception as e:  # noqa: BLE001 - surface as an in-page error
        sys.stderr.write(f"gh-scroll: fetch {url} failed: {e}\n")
        data = None
    if len(_CACHE) >= CACHE_MAX:
        for k in [k for k, (t, _) in _CACHE.items()
                  if now - t > CACHE_TTL]:
            _CACHE.pop(k, None)
    _CACHE[url] = (now, data)
    return data


def _gh_error(data):
    if isinstance(data, dict) and data.get("message"):
        msg = str(data["message"])
        if "rate limit" in msg.lower():
            return ("GitHub API rate limit reached (60/hour per IP). "
                    "Wait an hour, then refresh.")
        return msg
    return "It may not exist, or GitHub is unreachable."


# -- pages -----------------------------------------------------------------


def hub_page(ctx):
    featured = [s.strip() for s
                in str(ctx.config_value("featured")).split(",") if s.strip()]
    if not featured:
        featured = [s.strip() for s in DEFAULT_FEATURED.split(",") if s.strip()]
    rows = "".join(
        f'<div class="box"><a href="{SCHEME}://browse/{_esc(r)}">{_esc(r)}</a>'
        f' <span class="dim">· open the pseudo-site</span></div>'
        for r in featured)
    recent = _recent(ctx)
    recent_html = ""
    if recent:
        recent_html = "<h2>RECENT</h2>" + "".join(
            f'<div class="box"><a href="{SCHEME}://browse/{_esc(r)}">'
            f"{_esc(r)}</a></div>" for r in recent)
    return _page("Pseudo-Toe hub", f"""
<h1>GH·SCROLL</h1>
<p class="dim">A pseudo-site hub for GitHub repos. Nothing is hosted here —
every page is built live from the GitHub API, so a repo link becomes a
scrollable site.</p>
<form action="{SCHEME}://search/" method="get">
  <input type="text" name="q" value="" size="30">
  <input type="submit" value="search repos">
</form>
<h2>JUMP TO A REPO</h2>
<form action="{SCHEME}://browse/" method="get">
  <input type="text" name="owner_repo" value="" size="30">
  <input type="submit" value="open as a site">
</form>
<p class="dim">paste <b>owner/repo</b> or a full github.com link above.</p>
<h2>FEATURED</h2>
{rows}
{recent_html}
<p class="dim">Try <a href="{SCHEME}://search/?q=tiny+python">a search</a> or
<a href="{SCHEME}://user/torvalds">a user's repos</a>. Any
<a href="https://github.com/torvalds/linux">github.com repo link</a> opens
as a pseudo-site too.</p>
""")


def browse_page(ctx, path):
    parts = [urllib.parse.unquote(p) for p
             in path.split("?")[0].split("/") if p]
    if len(parts) < 2:
        return _error_page(f"{SCHEME}://browse needs "
                           "<b>owner/repo</b>.")
    owner, repo = parts[0], parts[1]
    _mark_recent(ctx, f"{owner}/{repo}")
    if len(parts) == 2:
        return _repo_page(ctx, owner, repo)
    return _path_page(ctx, owner, repo, "/".join(parts[2:]))


def _repo_page(ctx, owner, repo):
    info = _gh_get(ctx, f"/repos/{owner}/{repo}")
    if not isinstance(info, dict) or info.get("message"):
        return _error_page(f"Could not read <b>{owner}/{repo}</b>. "
                           f"{_esc(_gh_error(info))}")
    readme_html = ""
    readme = _gh_get(ctx, f"/repos/{owner}/{repo}/readme")
    if isinstance(readme, dict) and readme.get("content"):
        try:
            md = base64.b64decode(readme["content"]).decode("utf8", "replace")
            readme_html = f"<h2>README</h2>{_md_to_html(md)}"
        except Exception:  # noqa: BLE001 - a bad README is not fatal
            readme_html = ""
    files = _gh_get(ctx, f"/repos/{owner}/{repo}/contents")
    files_html = _listing_html(ctx, owner, repo, files)
    return _page(f"{owner}/{repo}", f"""
<h1>{_esc(owner)}/<span class="k">{_esc(repo)}</span></h1>
{_repo_header(info)}
<h2>FILES</h2>
{files_html}
{readme_html}
<p class="dim"><a href="{SCHEME}://hub">hub</a> ·
<a href="{SCHEME}://user/{_esc(owner)}">more from {_esc(owner)}</a></p>
""")


def _path_page(ctx, owner, repo, sub):
    quoted = urllib.parse.quote(sub, safe="/")
    data = _gh_get(ctx, f"/repos/{owner}/{repo}/contents/{quoted}")
    if data is None:
        return _error_page(f"Could not read <b>{_esc(sub)}</b> in "
                           f"<b>{owner}/{repo}</b>. {_esc(_gh_error(data))}")
    if isinstance(data, dict) and data.get("type") == "file":
        return _file_page(ctx, owner, repo, sub, data)
    if isinstance(data, list):
        return _dir_page(ctx, owner, repo, sub, data)
    return _error_page(_esc(_gh_error(data)) if isinstance(data, dict)
                       else f"<b>{_esc(sub)}</b> is not readable.")


def _dir_page(ctx, owner, repo, sub, files):
    return _page(f"{owner}/{repo}/{sub}", f"""
<h1>{_esc(owner)}/<span class="k">{_esc(repo)}</span></h1>
<p class="dim">{_crumbs(owner, repo, sub)}</p>
<h2>DIRECTORY</h2>
{_listing_html(ctx, owner, repo, files)}
<p class="dim"><a href="{SCHEME}://browse/{_esc(owner)}/{_esc(repo)}">
repo root</a></p>
""")


def _file_page(ctx, owner, repo, sub, data):
    content = None
    if data.get("encoding") == "base64" and data.get("content"):
        try:
            content = base64.b64decode(data["content"]).decode("utf8", "replace")
        except Exception:  # noqa: BLE001 - fall through to binary note
            content = None
    elif data.get("content"):
        content = data["content"]
    if content is None:
        content = f"[binary · {_human(data.get('size', 0))}]"
    ext = os.path.splitext(sub)[1].lstrip(".").upper() or "TEXT"
    return _page(f"{owner}/{repo} · {sub}", f"""
<h1>{_esc(owner)}/<span class="k">{_esc(repo)}</span> · {_esc(sub)}</h1>
<p class="dim">{_esc(ext)} · {_human(data.get('size', 0))}</p>
<pre>{_esc(content)}</pre>
<p class="dim"><a href="{SCHEME}://browse/{_esc(owner)}/{_esc(repo)}">
repo root</a></p>
""")


def search_page(ctx, path):
    q = _query(path).get("q", [""])[0].strip()
    body = f"""
<h1>GH·SEARCH</h1>
<form action="{SCHEME}://search/" method="get">
  <input type="text" name="q" value="{_esc(q)}" size="30">
  <input type="submit" value="search">
</form>
"""
    if not q:
        return _page("gh search", body + '<p class="dim">type a query above.</p>')
    data = _gh_get(ctx, "/search/repositories?q=" + urllib.parse.quote(q))
    items = data.get("items", []) if isinstance(data, dict) else []
    if not items:
        body += '<div class="box dim">no results.</div>'
    else:
        body += f'<p class="dim">{len(items)} results for "{_esc(q)}"</p>' + \
            "".join(_search_item(it) for it in items)
    return _page(f"search: {q}", body)


def user_page(ctx, path):
    parts = [p for p in path.split("?")[0].split("/") if p]
    if not parts:
        return hub_page(ctx)
    user = urllib.parse.unquote(parts[0])
    data = _gh_get(ctx, f"/users/{user}/repos?sort=updated&per_page=50")
    if isinstance(data, dict) or not isinstance(data, list):
        return _error_page(f"Could not list repos for <b>{_esc(user)}</b>. "
                           f"{_esc(_gh_error(data))}")
    rows = "".join(
        f'<div class="box"><a href="{SCHEME}://browse/{_esc(it.get("full_name", ""))}">'
        f"{_esc(it.get('name', ''))}</a> "
        f'<span class="dim">★ {it.get("stargazers_count", 0)}'
        + (f' · {_esc(it.get("language", ""))}' if it.get("language") else "")
        + f'</span><br><span class="dim">{_esc(it.get("description") or "")}</span></div>'
        for it in data)
    return _page(f"{user}'s repos", f"""
<h1>{_esc(user)}</h1>
<p class="dim">PUBLIC REPOS · SCROLL TO BROWSE</p>
{rows}
<p class="dim"><a href="{SCHEME}://hub">hub</a></p>
""")


# -- small render helpers --------------------------------------------------


def _repo_header(info):
    bits = []
    if info.get("stargazers_count") is not None:
        bits.append(f"★ {info['stargazers_count']}")
    if info.get("forks_count") is not None:
        bits.append(f"forks {info['forks_count']}")
    if info.get("language"):
        bits.append(info["language"])
    bits.append(f"default {info.get('default_branch', 'main')}")
    parts = [f'<p class="dim">{_esc(" · ".join(bits))}</p>']
    if info.get("description"):
        parts.append(f"<p>{_esc(info['description'])}</p>")
    return "".join(parts)


def _listing_html(ctx, owner, repo, files):
    if not isinstance(files, list):
        return _err_box(f"Could not list files for <b>{owner}/{repo}</b>. "
                        + _esc(_gh_error(files)))
    max_files = int(ctx.config_value("max_files") or 60)
    rows = []
    for f in files:
        ftype = f.get("type")
        name = f.get("name", "?")
        rel = _rel(f.get("path", ""), owner, repo)
        href = (f"{SCHEME}://browse/{_esc(owner)}/{_esc(repo)}/{_esc(rel)}")
        if ftype == "dir":
            rows.append(f'<div class="box dir"><a href="{href}">'
                        f"{_esc(name)}/</a></div>")
        else:
            rows.append(f'<div class="box file"><a href="{href}">'
                        f"{_esc(name)}</a> "
                        f'<span class="dim">{_human(f.get("size", 0))}</span>'
                        f"</div>")
    if len(rows) > max_files:
        rows = rows[:max_files]
        rows.append(f'<div class="box dim">… and {max_files} shown, '
                    f"more in the tree</div>")
    return "".join(rows) if rows else "<div class='box dim'>empty</div>"


def _crumbs(owner, repo, sub):
    out = [f'<a href="{SCHEME}://browse/{_esc(owner)}/{_esc(repo)}">'
           f"{_esc(repo)}</a>"]
    running = ""
    for p in sub.split("/"):
        running += "/" + p
        out.append(f' / <a href="{SCHEME}://browse/{_esc(owner)}/'
                   f'{_esc(repo)}{_esc(running)}">{_esc(p)}</a>')
    return "".join(out)


def _search_item(it):
    name = it.get("full_name", "")
    stars = it.get("stargazers_count", 0)
    lang = it.get("language") or ""
    desc = it.get("description") or ""
    return (f'<div class="box"><a href="{SCHEME}://browse/{_esc(name)}">'
            f"{_esc(name)}</a> <span class=\"dim\">★ {stars}"
            + (f" · {_esc(lang)}" if lang else "")
            + f'</span><br><span class="dim">{_esc(desc)}</span></div>')


def _md_to_html(md):
    out = []
    in_code = False
    for line in md.splitlines():
        if line.strip().startswith("```"):
            if in_code:
                out.append("</pre>")
                in_code = False
            else:
                out.append("<pre>")
                in_code = True
            continue
        if in_code:
            out.append(_esc(line))
            continue
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        if stripped.startswith("# "):
            out.append(f"<h3>{_esc(stripped[2:])}</h3>")
        elif stripped.startswith("## "):
            out.append(f"<h4>{_esc(stripped[3:])}</h4>")
        elif stripped.startswith("### "):
            out.append(f"<h5>{_esc(stripped[4:])}</h5>")
        elif re.match(r"^[-*] ", stripped):
            out.append(f'<div class="box">• {_md_inline(stripped[2:])}</div>')
        elif re.match(r"^\d+\. ", stripped):
            out.append(f'<div class="box">{_md_inline(stripped)}</div>')
        else:
            out.append(f"<p>{_md_inline(stripped)}</p>")
    if in_code:
        out.append("</pre>")
    return "\n".join(out)


def _md_inline(s):
    s = _esc(s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               r'<a href="\2">\1</a>', s)
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)


def _query(path):
    if "?" not in path:
        return {}
    _p, _, q = path.partition("?")
    return urllib.parse.parse_qs(q)


def _rel(path, owner, repo):
    prefix = f"{owner}/{repo}/"
    return path[len(prefix):] if path.startswith(prefix) else path


def _human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{int(n)}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _page(title, body):
    return (f"<!doctype html><html><head><title>{_esc(title)}</title>"
            f"<style>{STYLE}</style></head><body>{body}</body></html>")


def _error_page(msg):
    return _page("gh error", f'<h1>GH·ERROR</h1><div class="box">{msg}</div>'
                 f'<p class="dim"><a href="{SCHEME}://hub">back to the hub</a></p>')


def _err_box(msg):
    return f'<div class="box k">{msg}</div>'


def _mark_recent(ctx, repo):
    recent = list(ctx.settings.get("recent", []))
    if repo in recent:
        recent.remove(repo)
    recent.insert(0, repo)
    ctx.settings["recent"] = recent[:10]
    ctx.save_settings()


def _recent(ctx):
    return list(ctx.settings.get("recent", []))
