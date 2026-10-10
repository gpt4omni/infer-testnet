# Build from source

Everything is stdlib Python plus two packages. No compiler needed except
for the optional native bundles below.

## Run

Prerequisites: Python 3.11+ **with Tk** (the wallet GUI needs it).

- Windows: python.org installer — check "tcl/tk and IDLE" during install.
- Debian/Ubuntu: `sudo apt install python3-tk`
- Fedora: `sudo dnf install python3-tkinter`
- macOS: python.org installer (Tk included).

Then:

    pip install -r requirements.txt
    python3 wallet.py                        # desktop wallet
    python3 gateway_node.py --help           # node options
    python3 demo.py                          # smoke test, no network needed

Wallet flags: `--profile NAME` (separate wallets), `--gateway HOST:PORT`
`--pin ...` / `--no-tls` (network mode instead of local).

## Bundles (maintainers)

Linux one-dir bundle (tested): `pyinstaller -y wallet.spec` → `dist/`.

- Windows `.exe`: built by CI (`.github/workflows/build.yml`) on
  `windows-latest` — cross-building Windows binaries from Linux is
  unreliable, so don't. Trigger: manual dispatch or `v*` tag.
- Linux AppImage: same workflow on `ubuntu-22.04` (old glibc for
  compatibility) with `tools/make_icon.py` for the icon.
- macOS: not packaged yet — run from source (`python3 wallet.py` works
  with the python.org Tk build).

Artifacts land on the workflow run / GitHub release. Testnet funds only;
a packaged wallet is still the same test software.
