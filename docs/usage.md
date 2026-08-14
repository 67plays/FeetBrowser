# Usage

## Running

```bash
./run.sh                 # opens the welcome page
./run.sh https://example.com
./run.sh view-source:https://example.com
```

`run.sh` builds the Rust JS engine (`feetbrowser_engine`) into a local `.venv`
with maturin if it isn't importable yet, then runs the browser from that venv
(first run needs the Rust toolchain; maturin is installed into the venv
automatically). On NixOS it fetches a Python with Tk on the fly via
`nix-shell`. On other distros install Tk first (`python3-tk` on
Debian/Ubuntu, `python3-tkinter` on Fedora, `tk` on Arch). Once the extension
is built and installed for your interpreter, `python3 -m feetbrowser <url>`
works directly.

## Keyboard shortcuts

| Key | Action | Key | Action |
|-----|--------|-----|--------|
| `Ctrl-L` | focus address bar | `Ctrl-T` | new tab |
| `Ctrl-W` | close tab | `Ctrl-R` | reload |
| `Ctrl-D` | toggle bookmark | `about:bookmarks` | open bookmarks page |
| `Ctrl-H` | open `about:history` | `Ctrl-Tab` / `Ctrl-Shift-Tab` | next / previous tab |
| `PgUp` / `PgDn` / `Home` / `End` | page scroll controls | `Alt-←` / `Alt-→` | back / forward |
| `↑` / `↓` / wheel | scroll | `Esc` | blur address / input |
| middle / `Ctrl`-click | open link in new tab | `Ctrl-PgUp/Dn` | cycle tabs |

Type a URL in the address bar and press Enter, or type words to search
(DuckDuckGo HTML). Bare hosts without a scheme (`example.com:8080`,
`localhost:8000`) are assumed to be `https://`.

## Forms

Basic form support is wired up: `input[type=text/password]` fields are
focusable and typeable, checkboxes toggle, and submitting a form (clicking a
submit button or pressing Enter in a field) sends `GET` or `POST` to the form
`action`, which is resolved against the document's `<base href>` when one is
present.

## CLI reference

```bash
python3 -m feetbrowser --help             # full CLI reference
python3 -m feetbrowser --version          # print the version
python3 -m feetbrowser --toes                 # installed toes + status
python3 -m feetbrowser --toe-search <term>    # search the catalog
python3 -m feetbrowser --toe-install <name>   # install a toe
python3 -m feetbrowser --toe-uninstall <name> # uninstall a toe
python3 -m feetbrowser --toe-enable <name>    # enable a disabled toe
python3 -m feetbrowser --toe-disable <name>   # disable an installed toe
```
