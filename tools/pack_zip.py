r"""Pack, check and hash the Windows release zip.

    python tools/pack_zip.py pack  <dist dir> <name>   # dist/<name>/ -> dist/<name>.zip (+ .sha256)
    python tools/pack_zip.py check <zip>               # layout check only

The zip must use "/" entry names (Compress-Archive in Windows PowerShell 5
writes "\", which breaks unzip on other systems) and keep everything inside
one top-level <name>/ folder. The check reads the raw names from the archive:
zipfile.namelist() turns "\" into "/" on Windows, so it would hide the bug.
"""
import hashlib
import os
import sys
import zipfile


def pack(dist, name):
    src = os.path.join(dist, name)
    out = os.path.join(dist, name + ".zip")
    if not os.path.isdir(src):
        sys.exit(f"not a folder: {src}")
    if os.path.exists(out):
        os.remove(out)
    # strict_timestamps=False: files dated before 1980 (zeroed mtimes in
    # third-party downloads) are stored as 1980-01-01 instead of failing.
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
        for root, dirs, files in os.walk(src):
            dirs.sort()
            rel = os.path.relpath(root, dist).replace(os.sep, "/")
            zf.write(root, rel + "/")
            for f in sorted(files):
                zf.write(os.path.join(root, f), f"{rel}/{f}")
    return out


def check(path, name=None):
    if name is None:
        name = os.path.basename(path)[: -len(".zip")]
    with zipfile.ZipFile(path) as zf:
        raw = [i.orig_filename for i in zf.infolist()]
    if not raw:
        sys.exit(f"{path}: zip is empty")
    bad = [n for n in raw if "\\" in n]
    if bad:
        sys.exit(f"{path}: backslash entry names, e.g. {bad[:3]}")
    stray = [n for n in raw if not n.startswith(name + "/")]
    if stray:
        sys.exit(f"{path}: entries outside {name}/, e.g. {stray[:3]}")
    print(f"{path}: {len(raw)} entries, all under {name}/ with '/' separators")


def write_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    with open(path + ".sha256", "w", newline="\n") as f:
        f.write(f"{h.hexdigest()}  {os.path.basename(path)}\n")


def main(argv):
    if len(argv) == 3 and argv[0] == "pack":
        out = pack(argv[1], argv[2])
        check(out, argv[2])
        write_sha256(out)
    elif len(argv) == 2 and argv[0] == "check":
        check(argv[1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
