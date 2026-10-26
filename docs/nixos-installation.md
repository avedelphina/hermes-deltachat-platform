# NixOS Installation Guide

This guide covers installing the Delta Chat Hermes plugin on NixOS, including
voice call support (aiortc) which requires special handling because pip-installed
C extension wheels don't work reliably on NixOS.

## Basic Installation

### 1. Install deltachat-rpc-server

```bash
nix profile install nixpkgs#deltachat-rpc-server
echo 'DELTACHAT_RPC_SERVER=/home/$USER/.nix-profile/bin/deltachat-rpc-server' >> ~/.hermes/.env
```

### 2. Clone and enable the plugin

```bash
git clone https://github.com/avedelphina/hermes-deltachat-platform ~/.hermes/plugins/deltachat-platform
hermes plugins enable deltachat-platform
```

### 3. Create a Delta Chat account

```bash
python3 ~/.hermes/plugins/deltachat-platform/setup.py
```

### 4. Start the gateway

```bash
hermes gateway start
```

---

## Voice Call Support (aiortc)

Voice calls require `aiortc` and its C extension dependencies (`av`/PyAV,
`pylibsrtp`, etc.). These cannot be installed via `pip` on NixOS because the
manylinux wheels link against `libstdc++.so.6` which is not in NixOS's standard
library paths.

### Solution: nix-managed Python environment

Build a Python environment containing aiortc and all its dependencies
from **the nixpkgs your installed Hermes was built with** — not your system
`<nixpkgs>`. The env is loaded into the Hermes process, so it has to agree with
Hermes on three things:

- **Python version** — Hermes 0.21.6 and newer run on Python 3.14, 0.21.5 on
  3.12. Compiled modules built for one don't load into the other, and calls
  stay disabled.
- **glibc** — a newer nixpkgs links PyAV/ALSA against a newer glibc, and the
  import dies with ``version `GLIBC_2.4x' not found``.
- **cryptography** — Hermes imports its own `cryptography` first; a pyOpenSSL
  built for an older one then fails with
  `module 'lib' has no attribute 'GEN_EMAIL'` and the adapter cannot connect.

The expression reads the installed Hermes's locked flake from your `nix
profile` and takes nixpkgs from that flake's inputs. The `-o` flag creates a
symlink that acts as a **GC root**, keeping the store path alive through
`nix-collect-garbage`.

```bash
rm -f ~/.hermes/aiortc-env
nix build --impure -o ~/.hermes/aiortc-env --expr '
  let
    manifest = builtins.fromJSON (builtins.unsafeDiscardStringContext
      (builtins.readFile (builtins.getEnv "HOME" + "/.nix-profile/manifest.json")));
    hermes = builtins.head (builtins.filter
      (e: builtins.match ".*NousResearch/hermes-agent.*" (e.originalUrl or "") != null)
      (builtins.attrValues manifest.elements));
    pkgs = import (builtins.getFlake hermes.url).inputs.nixpkgs { system = builtins.currentSystem; };
    py = "python314";  # "python312" for Hermes 0.21.5
  in pkgs.${py}.withPackages (ps: [ ps.aiortc ps.numpy ])'
```

If Hermes comes from a NixOS or home-manager flake input instead of `nix
profile`, replace `hermes.url` with that input's locked ref (e.g.
`github:NousResearch/hermes-agent/v2026.9.24`).

Then add the environment's site-packages to Hermes's `PYTHONPATH` in `~/.hermes/.env`:

```bash
# Add this line (or prepend to existing PYTHONPATH); python3.12 for Hermes 0.21.5:
PYTHONPATH=/home/$USER/.hermes/aiortc-env/lib/python3.14/site-packages:/home/$USER/.hermes/python-packages
```

Restart Hermes — voice call support is now active.

### Verifying the installation

```bash
# Hermes's own interpreter, found through the wrapper's runtime closure
HERMES_PYTHON=$(nix-store -qR "$(readlink -f "$(which hermes)")" \
  | grep -- '-hermes-agent-env$')/bin/python3

PYTHONPATH=~/.hermes/aiortc-env/lib/python3.14/site-packages:~/.hermes/python-packages \
  $HERMES_PYTHON -c "import aiortc, av, numpy; from OpenSSL import crypto; print('aiortc', aiortc.__version__)"
```

### Keeping it up to date

**Rebuild the env after every Hermes update** — rerun the build command above.
Skipping it is what produces the `GEN_EMAIL` / `GLIBC_2.4x` errors.

### Reverting / removing voice call support

```bash
# Remove GC root (store path will be cleaned up on next nix-collect-garbage)
rm ~/.hermes/aiortc-env

# Remove PYTHONPATH addition from ~/.hermes/.env

# Remove the profile-level install if you ran nix profile install earlier
nix profile remove nixpkgs#python312Packages.aiortc 2>/dev/null || true

# Clean up nix store
nix-collect-garbage
```

---

## Why not `pip install aiortc`?

On NixOS, `pip install` works for pure-Python packages, but packages with C
extensions (like `av`/PyAV) link against system libraries (`libstdc++.so.6`,
`libav*`, etc.) at non-standard paths. The manylinux wheels assume
`/usr/lib` exists, which it doesn't on NixOS.

The `nix build` approach uses nixpkgs-compiled packages where all shared
library paths are encoded in the binary RPATH, so no `LD_LIBRARY_PATH`
hacks are needed and everything works correctly across reboots and
garbage collection cycles.

## Dev shell

For development and testing (not for the running Hermes daemon), a nix dev
shell with all dependencies is available:

```bash
cd ~/.hermes/plugins/deltachat-platform
nix develop          # enter dev shell with aiortc, deltachat2, pytest, etc.
nix develop --command pytest   # run tests
```
