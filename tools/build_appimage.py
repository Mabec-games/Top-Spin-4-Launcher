#!/usr/bin/env python3
"""Package the launcher as a self-contained x86_64 AppImage.

Bundles this machine's Python, its standard library, Tcl/Tk and every shared
library they need, including glibc and ld-linux. AppRun starts Python through
the bundled loader, so the AppImage does not depend on the host's glibc
(SteamOS, older distros). Xenia Edge is bundled (BSD-3-Clause, license
included) so players only supply their own Top Spin 4 ISO; no game data is
included.

  python3 tools/build_appimage.py --appimagetool PATH --runtime PATH [--xenia PATH]
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tkinter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "appimage"
APPDIR = BUILD / "AppDir"
DIST = ROOT / "dist"
APP_ID = "topspin4-launcher"
SHARE = APPDIR / "usr/share" / APP_ID
RUNTIME_LIBS = APPDIR / "usr/lib/runtime"
STDLIB_SKIP = ("site-packages", "test", "tests", "idlelib", "turtledemo", "ensurepip",
               "config-3*", "*.opt-1.pyc", "*.opt-2.pyc", "__phello__")
ASSETS = ("icon.png", "header.png", "steam_grid.png", "steam_wide.png", "steam_hero.png")
# Exported by AppRun, scrubbed again by the launcher before it starts Xenia.
BUNDLE_ENV = ("PYTHONHOME", "PYTHONDONTWRITEBYTECODE", "TCL_LIBRARY", "TK_LIBRARY")


def run(command, **kwargs):
    print("+", " ".join(str(c) for c in command))
    return subprocess.run([str(c) for c in command], check=True, **kwargs)


def version():
    text = (ROOT / "topspin4_launcher.py").read_text(encoding="utf-8")
    return re.search(r'^VERSION = "([^"]+)"', text, re.M).group(1)


def shared_libs(paths):
    """Every shared library (resolved path) the given ELF files load."""
    libs = {}
    for path in paths:
        out = subprocess.run(["ldd", str(path)], capture_output=True, text=True).stdout
        for line in out.splitlines():
            m = re.match(r"\s*(\S+) => (/\S+)", line) or re.match(r"\s*(/\S+ld-linux\S*)", line)
            if not m:
                continue
            name, target = (m.group(1), m.group(2)) if m.lastindex == 2 else \
                (os.path.basename(m.group(1)), m.group(1))
            libs[os.path.basename(name)] = Path(target).resolve()
    return libs


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--appimagetool", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--xenia", type=Path,
                        default=ROOT.parent / "xenia_edge_linux.appimage",
                        help="Xenia Edge AppImage to bundle")
    args = parser.parse_args()
    for path in (args.appimagetool, args.runtime, args.xenia,
                 ROOT / "licenses/XENIA_EDGE_LICENSE.txt"):
        if not path.is_file():
            sys.exit(f"missing: {path}")
    ver = version()
    out = DIST / f"Top-Spin-4-Launcher-Linux-v{ver}-x86_64.AppImage"

    shutil.rmtree(BUILD, ignore_errors=True)
    (APPDIR / "usr/bin").mkdir(parents=True)

    # Python interpreter and standard library (lib-dynload included).
    python = Path(sys.executable).resolve()
    shutil.copy2(python, APPDIR / "usr/bin/python3")
    pyver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    stdlib = Path(sysconfig.get_path("stdlib"))
    shutil.copytree(stdlib, APPDIR / "usr/lib" / pyver,
                    ignore=shutil.ignore_patterns(*STDLIB_SKIP), symlinks=False)

    # Tcl/Tk script libraries.
    tcl_lib = Path(tkinter.Tcl().eval("info library")).resolve()
    tk_lib = tcl_lib.parent / tcl_lib.name.replace("tcl", "tk")
    for lib in (tcl_lib, tk_lib):
        shutil.copytree(lib, APPDIR / "usr/lib" / lib.name)

    # Shared libraries, glibc and the dynamic loader.
    elves = [APPDIR / "usr/bin/python3"] + sorted((APPDIR / "usr/lib" / pyver).rglob("*.so"))
    RUNTIME_LIBS.mkdir(parents=True)
    libs = shared_libs(elves)
    for name, target in sorted(libs.items()):
        shutil.copy2(target, RUNTIME_LIBS / name)
    loader = next(n for n in libs if n.startswith("ld-linux"))
    print(f"bundled {len(libs)} shared libraries")

    # Launcher and art.
    SHARE.mkdir(parents=True)
    shutil.copy2(ROOT / "topspin4_launcher.py", SHARE / "topspin4_launcher.py")
    (SHARE / "assets").mkdir()
    for name in ASSETS:
        src = ROOT / "assets" / name
        if src.exists():
            shutil.copy2(src, SHARE / "assets" / name)

    # Bundled Xenia Edge (nested AppImage) and its license.
    (SHARE / "xenia").mkdir()
    shutil.copy2(args.xenia, SHARE / "xenia/xenia_edge_linux.appimage")
    (SHARE / "xenia/xenia_edge_linux.appimage").chmod(0o755)
    shutil.copytree(ROOT / "licenses", SHARE / "licenses")

    (APPDIR / "AppRun").write_text(f'''#!/bin/sh
# Start the bundled Python through the bundled loader so the host glibc is
# never used. The launcher removes TS4_BUNDLE_ENV variables before starting
# Xenia or other programs.
HERE="$(dirname "$(readlink -f "$0")")"
export PYTHONHOME="$HERE/usr"
export PYTHONDONTWRITEBYTECODE=1
export TCL_LIBRARY="$HERE/usr/lib/{tcl_lib.name}"
export TK_LIBRARY="$HERE/usr/lib/{tk_lib.name}"
export TS4_BUNDLE_ENV="{' '.join(BUNDLE_ENV)}"
exec "$HERE/usr/lib/runtime/{loader}" --library-path "$HERE/usr/lib/runtime" \\
  "$HERE/usr/bin/python3" -s "$HERE/usr/share/{APP_ID}/topspin4_launcher.py" "$@"
''')
    (APPDIR / "AppRun").chmod(0o755)

    desktop = APPDIR / f"{APP_ID}.desktop"
    desktop.write_text(f"""[Desktop Entry]
Type=Application
Name=Top Spin 4 Launcher
Comment=Play Top Spin 4 with Xenia Edge
Exec={APP_ID}
Icon={APP_ID}
Categories=Game;
Terminal=false
""")
    shutil.copy2(ROOT / "assets/icon.png", APPDIR / f"{APP_ID}.png")
    (APPDIR / ".DirIcon").symlink_to(f"{APP_ID}.png")
    if shutil.which("desktop-file-validate"):
        run(["desktop-file-validate", desktop])

    # Headless check with a near-empty environment: no host Python involved.
    test = run(["env", "-i", f"HOME={BUILD}", "PATH=/usr/bin:/bin", APPDIR / "AppRun",
                "--self-test"], capture_output=True, text=True).stdout
    print(test)
    if f"/share/{APP_ID}/xenia/" not in test:
        sys.exit("self-test did not pick up the bundled Xenia Edge")

    DIST.mkdir(exist_ok=True)
    out.unlink(missing_ok=True)
    run([args.appimagetool.resolve(), "--appimage-extract-and-run",
         "--runtime-file", args.runtime.resolve(), "--no-appstream", APPDIR, out],
        env=dict(os.environ, ARCH="x86_64"))
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    out.with_name(out.name + ".sha256").write_text(f"{digest}  {out.name}\n")
    print(f"{out.name}  {out.stat().st_size // 1024 // 1024} MB  sha256 {digest}")
    write_sha256sums()


def write_sha256sums():
    """SHA256SUMS.txt over every release payload in dist/ (AppImage and .exe)."""
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}"
             for p in sorted(DIST.iterdir()) if p.suffix in (".AppImage", ".exe")]
    (DIST / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
