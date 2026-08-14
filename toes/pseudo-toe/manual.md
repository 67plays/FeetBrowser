# Pseudo-Toe

**Pseudo-Toe** is a pseudo-site hub that turns GitHub repo links into
lightweight, scrollable sites rendered by **FeetBrowser** itself. Nothing is
hosted anywhere: there is no server, no HTML file, no static site. Every page
you see is assembled on the fly, inside the browser, from the GitHub API over
FeetBrowser's own HTTPS stack. The heavy github.com page is never fetched.

This makes it a perfect test bed for FeetBrowser's toe framework: it
exercises the `handle` hook (custom `gh://` scheme), toolbar buttons, and
per-toe settings — all with just one small module.

## What it does

Point the browser at a repo link and get a clean, scrollable site:

- the repo's name, description, stars, forks and language;
- the full file tree, browsable directory by directory;
- any text file, rendered as plain text with its size;
- the README, rendered from Markdown (headings, bullets, code blocks,
  inline links).

A **GH** toolbar button jumps straight to the hub.

## Pages

| URL | Page |
|-----|------|
| `gh://hub` | the hub: featured repos, jump-to-repo box, repo search, recently viewed |
| `gh://browse/<owner>/<repo>` | one repo as a site (README + file tree) |
| `gh://browse/<owner>/<repo>/<path>` | a subdirectory or a single file |
| `gh://search/?q=<term>` | GitHub repository search results |
| `gh://user/<name>` | every public repo of a user or org |

Any normal `https://github.com/<owner>/<repo>` link also works: with
`intercept_github` on (default), typing a repo link into the address bar
renders the pseudo-site instead of GitHub's page. `/tree/<branch>/…` and
`/blob/<branch>/…` links are understood too; everything else (e.g.
`github.com/features`, `/issues`) falls through to normal loading.

## How it works

Pseudo-Toe uses a single framework hook, `handle(url, tab)`. When a
navigation starts, the browser asks every installed toe whether it wants the
URL first. Pseudo-Toe answers:

1. **Custom scheme** — `gh://` URLs are parsed into hub / browse / search /
   user pages and rendered from GitHub API responses.
2. **Repo-link interception** — `https://github.com/<owner>/<repo>` (and
   `/tree` `/blob` variants) are rewritten into the same pseudo-site pages.

All data comes from the public GitHub API (`api.github.com`), read through
FeetBrowser's own network stack — the same sockets the browser uses to load
any web page. Responses are cached in memory for 5 minutes so repeat
browsing is fast and stays inside GitHub's anonymous rate limit
(60 requests/hour per IP).

The toe also:

- registers a toolbar button (`buttons` + `on_click`) that opens `gh://hub`;
- keeps a per-toe "recently viewed" list, persisted through `ctx.settings`.

## Configuration

Open `toehub://config/pseudo-toe` in the browser:

| Option | Default | What it controls |
|--------|---------|------------------|
| `intercept_github` | on | render github.com repo links as pseudo-sites |
| `featured` | 8 repos | comma-separated `owner/repo` list shown on the hub |
| `max_files` | 60 | cap on the per-directory file listing |

## Notes

- Binary files and files over ~1 MB (GitHub's blob cap) show a size note
  instead of content.
- With no API token, browsing is limited to GitHub's anonymous rate limit;
  failures render in-page as a friendly error, never a crash.